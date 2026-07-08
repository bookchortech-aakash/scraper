import requests
import time
import re
from parsel import Selector
import scriptkit

def run():
    print("🚀 Starting Bulletproof Deep Scraper for BookGanga...")
    
    url = "https://www.bookganga.com/eBooks/Common/LoadMoreBooKList"
    
    # We use standard requests, NO session tracking. This avoids triggering 
    # ASP.NET's state-based firewall blocks which caused the HTML redirects earlier.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest"
    }
    
    # The EXACT JSON payload required by BookGanga. 
    # DO NOT change "BookTitle" or any of the exact string spacing.
    book_filter = '{ "BookSearchTags":"" , "BookTitle":"" ,"BT":"" , "ISBN":"","AID":"0" , "LID":"0","CID":"0" , "PID":"0","FC":"0" , "EB":"0","EM":"0" , "FEB":"0","FEM":"0" , "cmdSearch":"","BookType":"1" , "LR":"0","UR":"0" , "CR":"0","Ath":"" , "Pub":"","BTitle":"" , "EId":"0","SelSortBy":"7" , "NEB":"0","NB":"0" , "IncOutOfStock":"1","SelCatOnly":"False","Alpha":"" } '
    
    start_index = 0
    batch_size = 50
    consecutive_errors = 0
    
    while True:
        print(f"\n--- Fetching Batch {start_index} to {start_index + batch_size} ---")
        params = {
            "StartIndex": start_index,
            "EndIndex": start_index + batch_size,
            "ListView": 2,
            "BookFilter": book_filter
        }
        
        try:
            # Use a fresh request every time, exactly like your successful CSV scraper did.
            response = requests.get(url, headers=headers, params=params, timeout=20)
            
            # WAF / Block protection check
            if "<html" in response.text.lower() or response.text.strip().startswith("<!DOCTYPE"):
                print("⚠️ Server returned HTML instead of JSON. IP might be temporarily rate-limited.")
                print(f"Preview: {response.text[:100]}...")
                print("Cooling down for 30 seconds...")
                time.sleep(30)
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    print("❌ Server continues to block requests. Stopping to protect IP.")
                    break
                continue
                
            try:
                html_data = response.json()
                consecutive_errors = 0 # Reset on success
            except Exception as e:
                print(f"⚠️ JSON Parse Error. Raw response preview: {response.text[:100]}")
                break
                
            if not html_data or not str(html_data).strip():
                print("✅ Catalog complete! (Empty response)")
                break
                
            sel = Selector(text=html_data, type='html')
            books = sel.css('.BookThumbBlock, div[class*="BookThumbBlock"]')
            
            if not books:
                print("✅ No more books found on grid.")
                break
                
            saved_count = 0
            
            for book in books:
                try:
                    title = book.css('.BookName::text').get(default="Unknown").strip()
                    
                    onclick = book.css('.BookThumbListBlock::attr(onclick)').get(default="")
                    prod_url = ""
                    if "location.href=" in onclick:
                        raw_url = onclick.split("location.href='")[1].split("'")[0]
                        prod_url = raw_url if raw_url.startswith("http") else "https://www.bookganga.com" + raw_url
                    
                    if not prod_url:
                        continue
                        
                    # DEDUPLICATION: Strip query parameters to guarantee unique database rows
                    clean_prod_url = prod_url.split('?')[0].strip()

                    # Deep crawl into the individual product page for category, ISBN, language
                    # Use a fresh request here too
                    prod_resp = requests.get(clean_prod_url, headers=headers, timeout=15)
                    prod_sel = Selector(text=prod_resp.text)
                    clean_text = re.sub(r'<[^>]+>', ' ', prod_resp.text)
                    
                    # Extract ISBN (Robust Regex)
                    isbn_match = re.search(r"ISBN[^\d]*([\d\-Xx]{10,17})", clean_text, re.IGNORECASE)
                    isbn = isbn_match.group(1).strip() if isbn_match else "N/A"
                    
                    # Extract Availability
                    avail = "In Stock" if "Add to Cart" in prod_resp.text or "Buy Now" in prod_resp.text else "Out of Stock"
                    
                    # Extract Language (Strict Word Boundaries to avoid "and" errors)
                    lang_match = re.search(r"\b(Marathi|English|Hindi|Sanskrit|Gujarati|Urdu|Tamil|Telugu|Bengali|Kannada)\b", clean_text, re.IGNORECASE)
                    language = lang_match.group(1).title() if lang_match else "Marathi"
                    
                    # Extract Category via Breadcrumbs
                    categories = prod_sel.css('.breadcrumb a::text, .Breadcrumb a::text, a[href*="CID="]::text').getall()
                    category = " > ".join([c.strip() for c in categories if c.strip() and c.strip().lower() != "home"])
                    if not category:
                        category = "Books"
                        
                    # Extract Publisher and Author using their specific query param links
                    author = prod_sel.css('a[href*="Ath="]::text').get(default="N/A").strip()
                    publisher = prod_sel.css('a[href*="Pub="]::text').get(default="N/A").strip()
                    
                    # Extract Price
                    price_text = "".join(book.css('.BookPrice::text').getall())
                    price_match = re.search(r"R\s*([0-9.,]+)", price_text)
                    price = price_match.group(1) if price_match else "N/A"
                    
                    book_record = {
                        "title": title,
                        "author": author,
                        "publisher": publisher,
                        "price": price,
                        "url": clean_prod_url, 
                        "isbn": isbn,
                        "language": language,
                        "category": category,
                        "availability": avail
                    }
                    
                    scriptkit.save("bookganga", [book_record], key_fields=["url"])
                    print(f"  -> ✅ Saved: {title[:20]:<20} | Lang: {language[:7]:<7} | Cat: {category[:15]:<15}")
                    saved_count += 1
                    
                    # Polite delay to prevent IP bans during deep crawl
                    time.sleep(0.3)
                    
                except Exception as e:
                    print(f"  -> ⚠️ Skipping item due to error: {e}")
            
            if saved_count == 0:
                print("✅ No valid books parsed in this batch. Stopping.")
                break

            start_index += batch_size
            time.sleep(1) 
            
        except requests.exceptions.RequestException as e:
            print(f"⚠️ Network Error on batch {start_index}: {e}")
            print("Waiting 10 seconds before retrying...")
            time.sleep(10)

if __name__ == "__main__":
    run()