import requests
import time
import re
import scriptkit 

def run():
    # We use Champaca's fast JSON endpoint to get the list of books
    base_json_url = "https://champaca.in/products.json?limit=250&page={}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    print("🚀 Starting Deep Scraper for Champaca (Saving directly to Dashboard DB)...")
    
    page = 1
    while True:
        print(f"\n--- Fetching List Page {page} ---")
        list_url = base_json_url.format(page)
        
        try:
            response = requests.get(list_url, headers=headers, timeout=15)
            data = response.json()
            products = data.get("products", [])
            
            if not products:
                print("✅ No more books found! Extraction complete.")
                break
                
            for prod in products:
                try:
                    # Title is guaranteed to be clean from the JSON
                    title = prod.get("title", "Unknown").strip()
                    
                    # Price is perfectly structured in the JSON
                    variants = prod.get("variants", [])
                    price = variants[0].get("price", "N/A") if variants else "N/A"
                    
                    # Availability is also perfectly structured in the JSON variants
                    is_available = variants[0].get("available") if variants else False
                    availability = "In Stock" if is_available else "Out of Stock"
                    
                    prod_url = f"https://champaca.in/products/{prod.get('handle')}"
                    
                    # Now we visit the page to grab the missing ISBN and Language from the description
                    prod_resp = requests.get(prod_url, headers=headers, timeout=15)
                    
                    # --- NEW BULLETPROOF TEXT EXTRACTION ---
                    clean_text = re.sub(r'<[^>]+>', ' ', prod_resp.text)
                    
                    # Language Extraction: Smart Fallback Logic
                    language_match = re.search(r"(?:Language|மொழி)[\s:]*([A-Za-z]+)", clean_text, re.IGNORECASE)
                    
                    if language_match:
                        language = language_match.group(1).strip()
                    else:
                        # If language isn't written on the page, check the hidden Shopify tags
                        tags_str = ", ".join(prod.get("tags", [])).lower()
                        if "tamil" in tags_str or "தமிழ்" in clean_text:
                            language = "Tamil"
                        elif "hindi" in tags_str or "हिंदी" in clean_text:
                            language = "Hindi"
                        elif "malayalam" in tags_str:
                            language = "Malayalam"
                        else:
                            # Default to English since Champaca is an English bookstore
                            language = "English"
                    
                    # ISBN Extraction using RegEx on the clean text
                    isbn_match = re.search(r"ISBN[^\w]*([\d\-Xx]{10,17})", clean_text, re.IGNORECASE)
                    isbn = isbn_match.group(1).strip() if isbn_match else "N/A"
                    
                    print(f"  -> ✅ Saved to DB: {title[:30]:<30} | {availability:<12} | ISBN: {isbn}")
                    
                    book_record = {
                        "title": title,
                        "price": str(price),
                        "url": prod_url.split('?')[0].strip(), # <--- Add this
                        "isbn": isbn,
                        "language": language,
                        "availability": availability
                    }
                    
                    # Push directly to your web UI
                    scriptkit.save("champaca", [book_record], key_fields=["url"])
                    
                    # Be polite to their server since we are hitting individual pages
                    time.sleep(0.2)
                    
                except Exception as e:
                    print(f"  -> ⚠️ Error processing {prod.get('handle')}: {e}")
            
            page += 1
            
        except Exception as e:
            print(f"⚠️ Error on page {page}: {e}")
            print("Waiting 5 seconds before retrying...")
            time.sleep(5)

if __name__ == "__main__":
    run()