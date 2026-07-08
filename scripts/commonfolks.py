import requests
import time
import re
import html
from parsel import Selector
from urllib.parse import urljoin
import scriptkit

def run():
    print("🚀 Starting CommonFolks Scraper (Global All-Books Strategy)...")
    
    # We hit the global catalog directly to get all 72,000+ books
    base_list_url = "https://www.commonfolks.in/books?f[page]={}&f[sort]=default&f[view]=grid"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    page = 1
    empty_pages = 0
    
    while True:
        print(f"\n--- Fetching Global Page {page} ---")
        current_listing_page = base_list_url.format(page)
        
        try:
            # Fetch the grid of books
            list_resp = requests.get(current_listing_page, headers=headers, timeout=20)
            list_sel = Selector(text=list_resp.text)
            
            # Extract all product links from the grid
            raw_book_links = list_sel.css('a[href*="/books/d/"]::attr(href)').getall()
            
            # Deduplicate links on the page level
            total_results_on_page = list(set([urljoin("https://www.commonfolks.in/", link) for link in raw_book_links if link]))
            
            if not total_results_on_page:
                print(f"⚠️ No books found on page {page}.")
                empty_pages += 1
                if empty_pages >= 3:
                    print("✅ Reached the absolute end of the 72,000+ book catalog.")
                    break
                page += 1
                continue
            
            empty_pages = 0
            
            for prod_url in total_results_on_page:
                
                # STRICT DEDUPLICATION: Strip ALL query parameters to avoid database duplicates
                clean_prod_url = prod_url.split('?')[0].strip()
                
                try:
                    p_resp = requests.get(clean_prod_url, headers=headers, timeout=15)
                    p_sel = Selector(text=p_resp.text)
                    
                    # THE BULLETPROOF METHOD: Strip all HTML tags so the entire page is one text string.
                    # html.unescape converts &#x20b9; back to the actual ₹ symbol so our regex can catch it!
                    raw_text = re.sub(r'<[^>]+>', ' ', p_resp.text)
                    clean_text = html.unescape(raw_text)
                    
                    # 1. Title
                    title = p_sel.css('h1::text, .book-title::text').get(default="").strip()
                    if not title:
                        title = p_sel.css('title::text').get(default="Unknown").split('|')[0].strip()
                        
                    # 2. Price 
                    # FIX: \b fails on ₹ because ₹ is not an ASCII letter. 
                    # We use (?:^|\s) for Rs/INR, and just look directly for ₹ anywhere.
                    all_prices = re.findall(r"(?:(?:^|\s)(?:Rs\.?|INR)|₹)\s*([0-9.,]+)", clean_text, re.IGNORECASE)
                    valid_prices = [p for p in all_prices if p not in ['0', '0.00', '30', '500']]
                    price = valid_prices[0] if valid_prices else "N/A"

                    # 3. ISBN
                    isbn_match = re.search(r"ISBN[^\d]*([\d\-Xx]{10,17})", clean_text, re.IGNORECASE)
                    isbn = isbn_match.group(1).strip() if isbn_match else "N/A"
                    
                    # 4. Language
                    lang_match = re.search(r"(?:Language|மொழி)[\s:]*([A-Za-z\u0B80-\u0BFF]+)", clean_text, re.IGNORECASE)
                    if lang_match:
                        language = lang_match.group(1).strip()
                    elif "தமிழ்" in clean_text or "Tamil" in clean_text:
                        language = "Tamil"
                    else:
                        language = "Unknown"
                        
                    # 5. Author
                    # FIX: Added 'Editor', 'No. of pages', 'Pages' to the stop-words to prevent bleeding
                    author_match = re.search(r"Author\s*:\s*(.*?)(?=\s*(?:Publisher|Editor|No\.?\s*of\s*pages|Pages|Other Specifications|₹|Add to cart))", clean_text, re.IGNORECASE)
                    author = author_match.group(1).strip() if author_match else "Unknown"
                        
                    # 6. Publisher
                    # FIX: Added the same stop-words here
                    pub_match = re.search(r"Publisher\s*:\s*(.*?)(?=\s*(?:Author|Editor|No\.?\s*of\s*pages|Pages|Other Specifications|₹|Add to cart))", clean_text, re.IGNORECASE)
                    publisher = pub_match.group(1).strip() if pub_match else "Unknown"
                    
                    # 7. Category / Subject
                    subject_match = re.search(r"Subject\s*:\s*(.*?)(?=\s*Published on|\s*Book Format|\s*Language|\s*₹)", clean_text, re.IGNORECASE)
                    if subject_match:
                        category = subject_match.group(1).strip()
                    else:
                        # Fallback to breadcrumbs if "Subject:" is missing
                        bc = p_sel.css('.breadcrumb a::text').getall()
                        category = bc[-1].strip() if bc else "Books"

                    book_record = {
                        "title": title,
                        "author": author,
                        "publisher": publisher,
                        "price": price,
                        "url": clean_prod_url,
                        "isbn": isbn,
                        "language": language,
                        "category": category
                    }
                    
                    scriptkit.save("commonfolks", [book_record], key_fields=["url"])
                    print(f"  -> ✅ Saved: {title[:25]:<25} | Price: ₹{price:<5} | Lang: {language[:7]}")
                    
                    # Polite delay
                    time.sleep(0.3)
                    
                except Exception as e:
                    print(f"  -> ⚠️ Error visiting {prod_url}: {e}")
            
            page += 1
                
        except Exception as e:
            print(f"⚠️ Error processing page {page}: {e}")
            time.sleep(5)
            page += 1

if __name__ == "__main__":
    run()