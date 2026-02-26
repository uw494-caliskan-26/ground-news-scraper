import time
import json
import sys
import os
import uuid
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from tqdm import tqdm
import trafilatura

BASE_URL = "https://ground.news"

def init_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--incognito")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

def collect_latest_stories(driver):
    driver.get(BASE_URL)
    print("Homepage loaded.")

    # Wait for main article to load
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "/html/body/main/article"))
        )
    except Exception as e:
        print(f"Timed out waiting for main article: {e}")
        return []

    # Scroll to trigger lazy loading
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(3)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)
    
    # Get latest topics from div[16]
    links = driver.find_elements(
        By.XPATH, f"/html/body/main/article/div[16]//a[contains(@href, '/article/')]"
    )
    if links:
        print(f"DEBUG: div[16] contains {len(links)} article links")

    stories = []
    seen_urls = set()

    for el in links:
        try:
            href = el.get_attribute("href")
            try:
                title = el.find_element(By.XPATH, ".//h4").text.strip()
            except:
                title = href

            if href and href not in seen_urls and title:
                stories.append({"title": title, "url": href})
                seen_urls.add(href)
        except:
            continue

    print(f"\nFound {len(stories)} stories on homepage:")
    
    for i, story in enumerate(stories):
        print(f"  [{i + 1}] {story['title']}")
        print(f"       {story['url']}")
    return stories


def fetch_article_data(driver, url):
    try:
        driver.get(url)
        time.sleep(1)
        print(f"\nPage loaded: {url}")

        article_data = {}

        # Generate story ID at the start
        story_id = f"GN_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"

        # Extract Title
        article_data['title'] = driver.find_element(By.ID, "titleArticle").text
        print(f"Title: {article_data['title']}")

        # Extract bias distribution
        article_data['total_source'] = driver.find_element(By.XPATH, "/html/body/main/div/article/div/div/div[4]/div[1]/div/div/span[2]").text
        article_data['leaning_left'] = driver.find_element(By.XPATH, "/html/body/main/div/article/div/div/div[4]/div[1]/div/span[2]").text
        article_data['leaning_right'] = driver.find_element(By.XPATH, "/html/body/main/div/article/div/div/div[4]/div[1]/div/span[4]").text
        article_data['center'] = driver.find_element(By.XPATH, "//*[@id='main']/div/article/div/div/div[4]/div[1]/div/span[6]").text

        # Extract perspective summaries
        article_data['left_points'] = []
        article_data['center_points'] = []
        article_data['right_points'] = []

        for side in ['left', 'center', 'right']:
            try:
                button = driver.find_element(By.ID, f"{side}-summary-button")
                if button.is_enabled() and button.is_displayed():
                    ActionChains(driver).move_to_element(button).click().perform()
                    time.sleep(2)
                    points = driver.find_elements(
                        By.XPATH,
                        "/html/body/main/div/article/div/div/div[1]/div[2]/div[3]/div/div/div[2]/div[1]/ul/li"
                    )
                    article_data[f'{side}_points'] = [p.text for p in points]
                    print(f"{side.capitalize()} points extracted: {len(article_data[f'{side}_points'])}")
            except Exception as e:
                print(f"{side.capitalize()} summary not available: {e}")

        # Load all stories
        stories_loaded = 0
        with tqdm(desc="Loading more stories", unit="batch") as pbar:
            while True:
                try:
                    more_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.ID, "more-stories"))
                    )
                    ActionChains(driver).move_to_element(more_btn).click().perform()
                    time.sleep(2)
                    stories_loaded += 1
                    pbar.update(1)
                    pbar.set_postfix({"Batches loaded": stories_loaded})
                except:
                    pbar.set_description("Finished loading stories")
                    break

        # Extract sources
        article_data['sources'] = []
        source_elements = driver.find_elements(By.ID, "article-summary")

        for source_element in tqdm(source_elements, desc="Processing sources", unit="source"):
            try:
                source = {}
                source['news_title'] = source_element.find_element(By.XPATH, ".//a/h4").text

                news_link_el = source_element.find_element(By.XPATH, "./div/a")
                source['news_link'] = news_link_el.get_attribute('href')

                try:
                    bias_el = source_element.find_element(By.XPATH, ".//a[contains(@id, 'article-source-bias')]/div")
                    source['bias'] = bias_el.text
                except:
                    source['bias'] = "unknown"

                downloaded = trafilatura.fetch_url(str(source['news_link']))
                if downloaded:
                    source['text'] = trafilatura.extract(downloaded)
                else:
                    source['text'] = None
                    print(f"  - Could not fetch: {source['news_link']}")

                article_data['sources'].append(source)

            except Exception as e:
                print(f"  - Error extracting source: {e}")
                continue

        print(f"Sources extracted: {len(article_data['sources'])}")

        # Build structured output
        structured_data = {
            'story_id': story_id,
            'metadata': {
                'title': article_data.get('title', ''),
                'timestamp': datetime.now().isoformat(),
                'url': url
            },
            'bias_distribution': {
                'total_sources': article_data.get('total_source', ''),
                'leaning_left': article_data.get('leaning_left', ''),
                'center': article_data.get('center', ''),
                'leaning_right': article_data.get('leaning_right', '')
            },
            'perspective_summaries': {
                'left': article_data.get('left_points', []),
                'center': article_data.get('center_points', []),
                'right': article_data.get('right_points', [])
            },
            'sources': article_data.get('sources', [])
        }

        # Save to JSON
        os.makedirs('json', exist_ok=True)
        json_filename = os.path.join('json', f"{story_id}.json")

        with open(json_filename, 'w', encoding='utf-8') as f:
            json.dump(structured_data, f, indent=2, ensure_ascii=False)
        print(f"Saved: {json_filename}")

        return structured_data

    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return None


def run_pipeline():
    driver = init_driver()

    try:
        # Step 1: collect all story URLs from homepage
        stories = collect_latest_stories(driver)

        if not stories:
            print("No stories found. Exiting.")
            return

        print(f"\nStarting pipeline for {len(stories)} stories...\n")

        # Step 2: scrape each story
        results = []
        for story in tqdm(stories, desc="Overall progress", unit="story"):
            result = fetch_article_data(driver, story['url'])
            if result:
                results.append(result)
            time.sleep(1)  # small buffer between stories

        print(f"\nPipeline complete. Successfully scraped {len(results)}/{len(stories)} stories.")

    except Exception as e:
        print(f"Pipeline error: {e}")
    finally:
        driver.quit()
        print("Browser closed.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Single URL mode
        driver = init_driver()
        try:
            fetch_article_data(driver, sys.argv[1])
        finally:
            driver.quit()
    else:
        # Full pipeline mode
        run_pipeline()