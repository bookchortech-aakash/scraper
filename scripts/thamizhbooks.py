import requests
from bs4 import BeautifulSoup
import csv
import time

# Target configuration
BASE_URL = "https://thamizhbooks.com/shop/"
HEADERS = {
    # Headers make our script look like a normal web browser to avoid getting blocked
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

def scrape_thamizh_books():
    print("Step 1: Fetching main shop page to determine total results...")
    
    # 1. First get the main page and find the total results/pagination
    response = requests.get(BASE_URL, headers=HEADERS)
    soup = BeautifulSoup(response.content, 'html.parser')
    
    # WooCommerce uses the 'page-numbers' class for pagination
    pagination_links = soup.select('ul.page-numbers li a.page-numbers:not(.next):not(.prev)')
    
    if pagination_links:
        # Extract the text of the last pagination number (e.g., '159')
        total_pages = int(pagination_links[-1].text.replace(',', '').strip())
    else:
        total_pages = 1
        
    print(f"Total pages found: {total_pages}")
    
    # Track URLs to completely avoid duplication
    processed_urls = set()
    all_books_data = []
    
    # 2. Use pagination at the time of loop initiation
    for page in range(1, total_pages + 1):
        print(f"\n--- Scraping Listing Page {page} of {total_pages} ---")
        
        # Construct the paginated URL
        page_url = f"{BASE_URL}page/{page}/" if page > 1 else BASE_URL
        page_res = requests.get(page_url, headers=HEADERS)
        page_soup = BeautifulSoup(page_res.content, 'html.parser')
        
        # Get all product links from the listing page
        product_elements = page_soup.select('.product a.woocommerce-LoopProduct-link')
        
        # 3. Create a loop to get each product details
        for product in product_elements:
            product_url = product.get('href')
            
            # Anti-Duplication Check
            if product_url in processed_urls:
                continue
            processed_urls.add(product_url)
            
            # 4. In this loop fetch data from product page url
            try:
                prod_res = requests.get(product_url, headers=HEADERS)
                prod_soup = BeautifulSoup(prod_res.content, 'html.parser')
                
                # Extract Title
                title_elem = prod_soup.select_one('h1.product_title')
                title = title_elem.text.strip() if title_elem else "Unknown Title"
                
                # Extract Price 
                price_elem = prod_soup.select_one('p.price')
                price = price_elem.text.strip().replace('\n', ' ') if price_elem else "N/A"
                
                # 5. Conditional for ISBN
                isbn_elem = prod_soup.select_one('.sku_wrapper .sku')
                if isbn_elem and isbn_elem.text.strip():
                    isbn = isbn_elem.text.strip()
                else:
                    # Conditional Fallback: Assume it's missing for now
                    isbn = "Not Available"
                
                # Extract Language and Everything Else (Attributes Table)
                language = "N/A"
                other_details = {}
                
                attr_table_rows = prod_soup.select('table.woocommerce-product-attributes tr')
                for row in attr_table_rows:
                    th = row.select_one('th')
                    td = row.select_one('td')
                    if th and td:
                        key = th.text.strip()
                        val = td.text.strip()
                        other_details[key] = val
                        
                        # Look for language (Checking for English or Tamil headers)
                        if "Language" in key or "மொழி" in key:
                            language = val
                            
                        # Conditional Fallback Execution: If ISBN wasn't in the SKU field, check the table
                        if isbn == "Not Available" and ("ISBN" in key or "ஐஎஸ்பிஎன்" in key):
                            isbn = val
                
                book_data = {
                    "Title": title,
                    "ISBN": isbn,
                    "Price": price,
                    "Language": language,
                    "URL": product_url,
                    "Everything_Else": str(other_details)
                }
                
                all_books_data.append(book_data)
                print(f"Extracted: {title[:35]}... | ISBN: {isbn}")
                
                # Polite delay of 1 second to avoid overloading their server
                time.sleep(1)
                
            except Exception as e:
                print(f"Failed to extract {product_url}. Error: {e}")
                
    # Save everything to a CSV file
    if all_books_data:
        keys = all_books_data[0].keys()
        with open('thamizhbooks_data.csv', 'w', newline='', encoding='utf-8-sig') as f:
            dict_writer = csv.DictWriter(f, fieldnames=keys)
            dict_writer.writeheader()
            dict_writer.writerows(all_books_data)
        print("\n✅ Scraping complete! Data saved securely to 'thamizhbooks_data.csv'")

if __name__ == "__main__":
    scrape_thamizh_books()