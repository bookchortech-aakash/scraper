import requests
import time
import re
from parsel import Selector
import scriptkit # <-- Imports your dashboard's internal database tool!

def run():
    # Panuval's default search endpoint. A blank search space (%20) returns the full catalog.
    base_url = "https://www.panuval.com/index.php?route=product/search&search=%20&limit=100&page={}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    print("🚀 Starting Deep Scraper for Panuval (Saving directly to Dashboard DB)...")
    
    page = 1
    while True:
        print(f"\n--- Fetching List Page {page} ---")
        list_url = base_url.format(page)
        
        try:
            response = requests.get(list_url, headers=headers, timeout=15)
            sel = Selector(text=response.text)
            
            # Extract all product links from the grid
            product_links = sel.css('.product-thumb .image a::attr(href), .product-layout .name a::attr(href)').getall()
            product_links = list(set(product_links)) # Deduplicate links
            
            if not product_links:
                print("✅ No more books found on grid! Extraction complete.")
                break
                
            for prod_url in product_links:
                try:
                    prod_resp = requests.get(prod_url, headers=headers, timeout=15)
                    prod_sel = Selector(text=prod_resp.text)
                    
                    # 1. Title Extraction (Fixed duplication issue)
                    # Using "h1 ::text" prevents it from grabbing nested tags twice!
                    title_parts = prod_sel.css('h1 ::text').getall()
                    title = "".join(title_parts).strip()
                    if not title:
                        title = prod_sel.css('title::text').get(default="").split('|')[0].strip()
                    
                    # --- NEW BULLETPROOF TEXT EXTRACTION ---
                    # We strip away all HTML tags (<p>, <div>, <li>) so the page becomes one giant 
                    # clean paragraph. This makes regex matching practically impossible to fail!
                    content_area = prod_sel.css('#content, .product-layout, .container').get(default=prod_resp.text)
                    clean_text = re.sub(r'<[^>]+>', ' ', content_area)
                    
                    # 2. Price Extraction (Fixed N/A issue)
                    # Now it just looks for the ₹ or Rs. symbol anywhere in that massive clean text!
                    price_match = re.search(r"(?:Rs\.?|₹)\s*([0-9.,]+)", clean_text)
                    price = price_match.group(1) if price_match else "N/A"
                    
                    # 3. Language Extraction
                    language_match = re.search(r"(?:Language|மொழி)[\s:]*([A-Za-z\u0B80-\u0BFF]+)", clean_text, re.IGNORECASE)
                    lang_tamil_match = re.search(r"(தமிழ்|Tamil)", clean_text, re.IGNORECASE)
                    
                    language = "Unknown"
                    if language_match:
                        language = language_match.group(1).strip()
                    elif lang_tamil_match:
                        language = "Tamil"

                    # 4. ISBN Extraction
                    isbn_match = re.search(r"ISBN[\s:]*([\d\-Xx]{10,17})", clean_text, re.IGNORECASE)
                    isbn = isbn_match.group(1) if isbn_match else "N/A"
                    
                    title_str = str(title) if title else "Unknown"
                    isbn_str = str(isbn)
                    lang_str = str(language)
                    
                    print(f"  -> ✅ Saved to DB: {title_str[:35]:<35} | Price: {str(price):<8} | ISBN: {isbn_str}")
                    
                    # --- Save directly to your Web UI Database ---
                    book_record = {
                        "title": title_str,
                        "price": str(price),
                        "url": prod_url.split('?')[0].strip(), # <--- Add this
                        "isbn": isbn_str,
                        "language": lang_str
                    }
                    
                    scriptkit.save("panuval", [book_record], key_fields=["url"])
                    
                    # Be polite to the server
                    time.sleep(0.2) 
                    
                except Exception as e:
                    print(f"  -> ⚠️ Error visiting {prod_url}: {e}")
            
            # Move to the next page of the grid
            page += 1
            
        except Exception as e:
            print(f"⚠️ Error on page {page}: {e}")
            print("Waiting 5 seconds before retrying...")
            time.sleep(5)

if __name__ == "__main__":
    run()