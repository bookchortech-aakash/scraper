import requests
import csv
import time
import re
from parsel import Selector

def run():
    url = "https://www.bookganga.com/eBooks/Common/LoadMoreBooKList"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest"
    }
    
    # Exact filter payload from the website to trigger the "All Books" query
    book_filter = '{ "BookSearchTags":"" , "BookTitle":"" ,"BT":"" , "ISBN":"","AID":"0" , "LID":"0","CID":"0" , "PID":"0","FC":"0" , "EB":"0","EM":"0" , "FEB":"0","FEM":"0" , "cmdSearch":"","BookType":"1" , "LR":"0","UR":"0" , "CR":"0","Ath":"" , "Pub":"","BTitle":"" , "EId":"0","SelSortBy":"7" , "NEB":"0","NB":"0" , "IncOutOfStock":"1","SelCatOnly":"False","Alpha":"" } '

    # STARTING FROM SCRATCH
    start_index = 0
    batch_size = 50
    
    print("🚀 Starting BookGanga API Extraction from the beginning...")
    
    # "w" (write mode) completely empties the old file automatically!
    with open("bookganga_full_catalog.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Title", "Price", "URL", "Image_URL"])
        
        while True:
            print(f"Fetching books {start_index} to {start_index + batch_size}...")
            
            params = {
                "StartIndex": start_index,
                "EndIndex": start_index + batch_size,
                "ListView": 2,
                "BookFilter": book_filter
            }
            
            try:
                response = requests.get(url, headers=headers, params=params, timeout=15)
                html_data = response.json()
                
                if not html_data.strip():
                    print("✅ End of catalog reached!")
                    break
                    
                sel = Selector(text=html_data, type='html')
                books = sel.css('.BookThumbBlock, div[class*="BookThumbBlock"]')
                
                if not books:
                    print("✅ No more books found. Extraction complete!")
                    break
                    
                for book in books:
                    title = book.css('.BookName::text').get(default="").strip()
                    
                    price_text = "".join(book.css('.BookPrice::text').getall())
                    price_match = re.search(r"R\s*([0-9.,]+)", price_text)
                    price = price_match.group(1) if price_match else price_text.strip()
                    
                    onclick = book.css('.BookThumbListBlock::attr(onclick)').get(default="")
                    book_url = ""
                    if "location.href=" in onclick:
                        book_url = onclick.split("location.href='")[1].split("'")[0]
                        if book_url.startswith("/"):
                            book_url = "https://www.bookganga.com" + book_url
                            
                    image_url = book.css('img::attr(src)').get(default="")
                    if image_url.startswith("/"):
                        image_url = "https://www.bookganga.com" + image_url
                        
                    writer.writerow([title, price, book_url, image_url])
                    
                start_index += batch_size
                time.sleep(0.5) 
                
            except Exception as e:
                print(f"⚠️ Error fetching batch {start_index}: {e}")
                print("Waiting 5 seconds before retrying...")
                time.sleep(5)

if __name__ == "__main__":
    run()