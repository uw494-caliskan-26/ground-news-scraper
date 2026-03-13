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
RESULTS_FILE = os.path.join(OUTPUT_DIR, "results.csv")
DEBUG_DIR = "debug"
MAX_TOPICS = 5
HOMEPAGE_SCAN_LIMIT = 150  # gather more than needed, then trim to 5

SOURCE_CSV_FILE = os.path.join("dataset", "sources.csv")
SOURCE_CSV_FIELDS = ["story_id", "source_news_title", "source_news_link", "source_text", "source_bias"]

CSV_FIELDS = [
    "story_id", "title", "url", "timestamp",
    "total_sources", "leaning_left", "center", "leaning_right",
    "left_points", "center_points", "right_points",
    "sources",
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
    """
    Prefer the actual external publisher URL.
    Avoid Ground News internal pages like /interest/ and /article/.
    """
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

            # Prefer non-Ground News links
            if not is_ground_news_url(href):
                score += 100

            # Penalize internal Ground News pages
            if is_internal_source_page(href):
                score -= 100

            # Prefer explicit article/open/read wording
            if "read article" in text or "read full article" in text:
                score += 40
            if "read article" in aria or "read full article" in aria:
                score += 40
            if "read article" in title or "read full article" in title:
                score += 40

            # Slightly prefer anchors with visible text
            if text:
                score += 5

            candidates.append((score, href, text, aria, title))
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

    # remove homepage add-ons like "48% Left coverage: 52 sources"
    title = re.sub(r"\b\d+%\s+(left|center|right)\s+coverage:.*$", "", title).strip()
    title = re.sub(r"\b\d+\s+sources\b.*$", "", title).strip()

    # normalize punctuation/spacing
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    return title

def _flatten(data):
    metadata = data.get("metadata", {})
    bias = data.get("bias_distribution", {})
    summaries = data.get("perspective_summaries", {})

    return {
        "story_id": data.get("story_id", ""),
        "title": metadata.get("title", ""),
        "url": metadata.get("url", ""),
        "timestamp": metadata.get("timestamp", ""),
        "total_sources": bias.get("total_sources", ""),
        "leaning_left": bias.get("leaning_left", ""),
        "center": bias.get("center", ""),
        "leaning_right": bias.get("leaning_right", ""),
        "left_points": " | ".join(summaries.get("left", [])),
        "center_points": " | ".join(summaries.get("center", [])),
        "right_points": " | ".join(summaries.get("right", [])),
        "sources": json.dumps(data.get("sources", []), ensure_ascii=False),
    }


def split_points(value):
    if not value or value is None:
        return []
    if not isinstance(value, str):
        return []
    return [p for p in value.split(" | ") if p]


def load_results():
    results = []
    seen_urls = set()
    seen_titles = set()

    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                raw_url = row.get("url", "")
                norm_url = normalize_url(raw_url)
                norm_title = normalize_title(row.get("title", ""))

                if norm_url:
                    seen_urls.add(norm_url)
                if norm_title:
                    seen_titles.add(norm_title)

                try:
                    sources = json.loads(row["sources"]) if row.get("sources") else []
                except Exception:
                    sources = []

                results.append({
                    "story_id": row.get("story_id", ""),
                    "metadata": {
                        "title": row.get("title", ""),
                        "url": norm_url,
                        "timestamp": row.get("timestamp", "")
                    },
                    "bias_distribution": {
                        "total_sources": row.get("total_sources", ""),
                        "leaning_left": row.get("leaning_left", ""),
                        "center": row.get("center", ""),
                        "leaning_right": row.get("leaning_right", ""),
                    },
                    "perspective_summaries": {
                        "left": split_points(row.get("left_points")),
                        "center": split_points(row.get("center_points")),
                        "right": split_points(row.get("right_points")),
                    },
                    "sources": sources,
                })

    # Also scan json files in case CSV is stale/incomplete
    for path in glob.glob(os.path.join(OUTPUT_DIR, "*.json")):
        if os.path.basename(path) == "results.csv":
            continue
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

    print(f"Resuming: {len(results)} stories already scraped.")
    print(f"Loaded {len(seen_urls)} seen URLs.")
    print(f"Loaded {len(seen_titles)} seen title fingerprints.")
    return results, seen_urls, seen_titles


def save_results(results):
    if not results:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for data in results:
            writer.writerow(_flatten(data))
    print(f"[Saved {len(results)} total results -> {RESULTS_FILE}]")

def append_sources_to_csv(story_data):
    """
    Given a structured story (as from fetch_article_data), append each source to dataset/sources.csv
    """
    if not story_data or "sources" not in story_data:
        return

    os.makedirs(os.path.dirname(SOURCE_CSV_FILE), exist_ok=True)
    file_exists = os.path.exists(SOURCE_CSV_FILE)

    with open(SOURCE_CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SOURCE_CSV_FIELDS)

        # write header only if file didn't exist
        if not file_exists:
            writer.writeheader()

        for src in story_data["sources"]:
            writer.writerow({
                "story_id": story_data["story_id"],
                "source_news_title": src.get("news_title", ""),
                "source_news_link": src.get("news_link", ""),
                "source_text": src.get("text", ""),
                "source_bias": src.get("bias", ""),
            })

def init_driver():
    options = webdriver.ChromeOptions()
    is_ci = os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS") or True  # force headless

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

    # Wait for homepage links to appear
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

    # Scroll more aggressively so more cards render
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

            stories.append({
                "title": title,
                "url": href,
            })
            seen_urls.add(href)

            if len(stories) >= HOMEPAGE_SCAN_LIMIT:
                break

        except StaleElementReferenceException:
            continue
        except Exception:
            continue

    print(f"Collected {len(stories)} candidate homepage stories.")
    for i, s in enumerate(stories[:15], 1):
        print(f"[{i}] {s['title']}")
        print(f"    {s['url']}")
    if len(stories) > 15:
        print(f"... and {len(stories) - 15} more")

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


def extract_perspective_points(driver, side):
    selectors = [
        (By.ID, f"{side}-summary-button"),
        (By.XPATH, f"//button[contains(@id, '{side}-summary-button')]"),
    ]

    for by, sel in selectors:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((by, sel))
            )
            if not safe_click(driver, btn):
                continue

            time.sleep(1)

            point_selectors = [
                (By.XPATH, "//ul/li"),
                (By.XPATH, "//div[contains(@class, 'summary')]//li"),
            ]

            for pby, psel in point_selectors:
                pts = driver.find_elements(pby, psel)
                cleaned = [p.text.strip() for p in pts if p.text.strip()]
                if cleaned:
                    return cleaned[:10]
        except Exception:
            continue

    return []


def extract_source_cards(driver):
    selectors = [
        (By.XPATH, "//*[@id='article-summary']"),
        (By.XPATH, "//div[@id='article-summary']"),
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
                    key = el.id
                except Exception:
                    key = str(id(el))
                if key not in seen:
                    seen.add(key)
                    cards.append(el)
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

        left_points = extract_perspective_points(driver, "left")
        center_points = extract_perspective_points(driver, "center")
        right_points = extract_perspective_points(driver, "right")

        print(f"Left points: {len(left_points)}")
        print(f"Center points: {len(center_points)}")
        print(f"Right points: {len(right_points)}")

        expand_all_sources(driver)

        source_cards = extract_source_cards(driver)
        print(f"Source cards found: {len(source_cards)}")

        sources = []
        seen_links = set()

        for card in tqdm(source_cards, desc="Processing sources", unit="source"):
            try:
                link = None
                title_text = ""

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
            "perspective_summaries": {
                "left": left_points,
                "center": center_points,
                "right": right_points,
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
    results, seen_urls, seen_titles = load_results()

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
        print("No new homepage topics found in collected pool.")
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
                results.append(result)
                seen_urls.add(story_url)
                seen_titles.add(normalize_title(result["metadata"]["title"]))
                save_results(results)
                success_count += 1
        except KeyboardInterrupt:
            print("\nInterrupted. Saving progress...")
            save_results(results)
            try:
                driver.quit()
            except Exception:
                pass
            raise
        except Exception as e:
            print(f"Topic-level error for {story_url}: {e}")
            save_results(results)
        finally:
            try:
                driver.quit()
            except Exception:
                pass

        time.sleep(1)

    print(f"\nDone. Scraped {success_count} new homepage topic(s) this run.")
    print(f"Stored {len(results)} total topic records.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        driver = init_driver()
        try:
            fetch_article_data(driver, normalize_url(sys.argv[1]))
        finally:
            driver.quit()
    else:
        run_pipeline()