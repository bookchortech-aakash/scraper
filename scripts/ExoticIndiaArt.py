import requests
import time
import re
from parsel import Selector
import scriptkit

def run():
    base_url = "https://www.exoticindiaart.com/book/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    print("🚀 Stage 1: Discovering Categories...")
    try:
        response = requests.get(base_url, headers=headers, timeout=20)
        sel = Selector(text=response.text)
        category_links = sel.css('a[href*="/book/"]::attr(href)').getall()
        category_links = list(set([l for l in category_links if "/book/" in l and l.count('/') > 2]))
        print(f"✅ Found {len(category_links)} categories to explore.")
    except Exception as e:
        print(f"⚠️ Could not fetch categories: {e}")
        return

    for cat_url in category_links:
        if not cat_url.startswith("http"):
            cat_url = "https://www.exoticindiaart.com" + cat_url
        
        # EXTRACT CORRECT CATEGORY: Take it from the Category URL, not the Product URL
        cat_parts = [p for p in cat_url.split('/') if p and p not in ['https:', 'http:', 'www.exoticindiaart.com', 'com', 'org', 'book']]
        category_name = " > ".join(cat_parts).title() if cat_parts else "Books"
        
        page = 1
        previous_links = set() # <--- INFINITE LOOP PROTECTION
        
        while True:
            print(f"\n--- Scraping Category: {category_name} | Page {page} ---")
            params = {"page": page} if page > 1 else {}
            
            try:
                resp = requests.get(cat_url, headers=headers, params=params, timeout=20)
                sel = Selector(text=resp.text)
                product_links = sel.css('a[href*="/book/details/"]::attr(href)').getall()
                
                current_links = set(product_links)
                
                if not current_links:
                    print("✅ No products on this page. Moving to next category.")
                    break
                    
                # If the website ignores pagination and returns Page 1 again, break the loop!
                if current_links == previous_links:
                    print("✅ Reached the end (Site repeating items). Moving to next category.")
                    break
                    
                previous_links = current_links
                
                for prod_url in current_links:
                    if not prod_url.startswith("http"):
                        prod_url = "https://www.exoticindiaart.com" + prod_url
                    
                    # Deduplication: Strip query params
                    clean_prod_url = prod_url.split('?')[0].strip()
                    
                    try:
                        item_resp = requests.get(prod_url, headers=headers, timeout=15)
                        item_sel = Selector(text=item_resp.text)
                        content = re.sub(r'<[^>]+>', ' ', item_resp.text)
                        
                        title = item_sel.css('h1::text').get(default="Unknown").strip()
                        price_match = re.search(r"(?:Price|Rs\.?|₹|\$)\s*([\d,.]+)", content)
                        price = price_match.group(1) if price_match else "N/A"
                        isbn_match = re.search(r"(?:ISBN|ISBN-13)[^\d]*([\d\-Xx]{10,17})", content, re.IGNORECASE)
                        isbn = isbn_match.group(1).strip() if isbn_match else "N/A"
                        
                        # Strict word boundary for languages
                        lang_match = re.search(r"\b(English|Hindi|Sanskrit|Marathi|Tamil|Telugu|Malayalam|Bengali)\b", content, re.IGNORECASE)
                        language = lang_match.group(1).strip() if lang_match else "English"
                        
                        book_record = {
                            "title": title,
                            "price": price,
                            "url": clean_prod_url, # Strict deduplication
                            "isbn": isbn,
                            "language": language,
                            "category": category_name
                        }
                        
                        scriptkit.save("exoticindia", [book_record], key_fields=["url"])
                        print(f"  -> ✅ Saved: {title[:20]:<20} | Cat: {category_name[:15]} | Lang: {language}")
                        time.sleep(0.3)
                        
                    except Exception as e:
                        print(f"  -> ⚠️ Error visiting {prod_url}: {e}")
                
                page += 1
            except Exception as e:
                print(f"⚠️ Error on page {page}: {e}")
                break

if __name__ == "__main__":
    run()