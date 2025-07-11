import requests
from bs4 import BeautifulSoup
import sqlite3
from datetime import datetime, timedelta
import time
from urllib.parse import urljoin, urlparse, urlunparse
import base64
import os
import argparse
from collections import OrderedDict

def setup_database(db_name, enable_resume):
    """
    Sets up a SQLite database file and creates tables.
    The attachments table is always created to store links.

    Args:
        db_name (str): The name of the database file.
        enable_resume (bool): Flag to determine if the progress tracking table should be created.

    Returns:
        sqlite3.Connection: The connection object to the database.
    """
    conn = sqlite3.connect(db_name, check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS press_releases (
            url TEXT PRIMARY KEY,
            release_id TEXT,
            date TEXT NOT NULL,
            sequence_in_day INTEGER,
            language TEXT NOT NULL,
            title TEXT,
            timestamp TEXT,
            content TEXT,
            corresponding_url TEXT
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_release_id ON press_releases (release_id);')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attachments (
            attachment_url TEXT PRIMARY KEY,
            release_url TEXT NOT NULL,
            file_type TEXT,
            base64_data TEXT,
            FOREIGN KEY (release_url) REFERENCES press_releases (url)
        )
    ''')
    
    if enable_resume:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_pages (
                page_url TEXT PRIMARY KEY
            )
        ''')

    conn.commit()
    print(f"Database '{db_name}' is set up.")
    return conn

def insert_data(conn, release_data, attachments_list):
    """
    Inserts a press release and its attachments into the database in a single transaction.
    """
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO press_releases (url, release_id, date, sequence_in_day, language, title, timestamp, content, corresponding_url)
            VALUES (:url, :release_id, :date, :sequence_in_day, :language, :title, :timestamp, :content, :corresponding_url)
        ''', release_data)
        
        if attachments_list:
            for attachment in attachments_list:
                cursor.execute('''
                    INSERT OR IGNORE INTO attachments (attachment_url, release_url, file_type, base64_data)
                    VALUES (:attachment_url, :release_url, :file_type, :base64_data)
                ''', attachment)
            
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error during transaction: {e}")
        conn.rollback()

def download_and_encode(url):
    """Downloads a file from a URL and returns its Base64 encoded string."""
    try:
        print(f"        -> Downloading: {url}")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return base64.b64encode(response.content).decode('utf-8')
    except requests.exceptions.RequestException as e:
        print(f"      -> Failed to download attachment {url}: {e}")
        return None

def get_press_release_content(url, conn, should_download_attachments):
    """
    Fetches and parses a press release page, records attachment links, and optionally downloads them.
    """
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        title = soup.find('div', id='PRHeadline').get_text(strip=True) if soup.find('div', id='PRHeadline') else 'No Title'
        timestamp = soup.find('div', id='PRDate').get_text(strip=True) if soup.find('div', id='PRDate') else 'No Timestamp'
        content_div = soup.find('span', id='pressrelease')
        content = content_div.get_text('\n', strip=True) if content_div else 'No Content'

        corresponding_url = None
        lang_link_img = soup.find('img', id='hdrTCLnk') or soup.find('img', id='hdrENLnk')
        if lang_link_img and lang_link_img.parent.name == 'a':
            relative_link = lang_link_img.parent.get('href')
            if relative_link:
                abs_link = urljoin(url, relative_link)
                parsed_url = urlparse(abs_link)
                corresponding_url = urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, '', '', ''))

        attachments = []
        right_content = soup.find('div', id='rightContent')
        if right_content:
            attachment_links = right_content.select('a.fancybox, a.attach_text')
            unique_attachment_urls = {urljoin(url, link.get('href')) for link in attachment_links if link.get('href')}

            for full_attachment_url in unique_attachment_urls:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM attachments WHERE attachment_url = ?", (full_attachment_url,))
                if cursor.fetchone():
                    print(f"        -> Attachment link already in DB, skipping: {full_attachment_url}")
                    continue

                allowed_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx')
                file_ext = os.path.splitext(urlparse(full_attachment_url).path)[1].lower()
                file_type = file_ext[1:] if file_ext else 'unknown'
                base64_data = None

                if should_download_attachments and file_ext in allowed_extensions:
                    base64_data = download_and_encode(full_attachment_url)
                else:
                    print(f"        -> Recording link for attachment type ({file_type}): {full_attachment_url}")

                attachments.append({
                    'attachment_url': full_attachment_url, 'release_url': url,
                    'file_type': file_type, 'base64_data': base64_data
                })
                        
        return title, timestamp, content, attachments, corresponding_url
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching press release page {url}: {e}")
        return None, None, None, [], None

def crawl_day_page(date, conn, download_attachments, enable_resume, base_url="https://www.info.gov.hk/gia/general"):
    """
    Crawls the summary page for a specific day and inserts findings into the DB.
    """
    day_str, year_month_str = date.strftime("%d"), date.strftime("%Y%m")
    urls_to_check = {'EN': f"{base_url}/{year_month_str}/{day_str}.htm", 'ZH': f"{base_url}/{year_month_str}/{day_str}c.htm"}

    for lang, day_url in urls_to_check.items():
        if enable_resume:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_pages WHERE page_url = ?", (day_url,))
            if cursor.fetchone():
                print(f"Skipping already processed page: {day_url}")
                continue

        print(f"Checking {lang} URL: {day_url}")
        try:
            response = requests.get(day_url, timeout=10)
            if response.status_code == 404:
                if enable_resume:
                    conn.execute("INSERT OR IGNORE INTO processed_pages (page_url) VALUES (?)", (day_url,))
                    conn.commit()
                continue
            response.raise_for_status()

            soup = BeautifulSoup(response.content, 'html.parser')
            release_links = soup.select('ul.news_list a, td.e_title a, td.c_title a, a.NEW')
            
            print(f"  -> Found {len(release_links)} release(s).")
            for seq, link in enumerate(release_links, 1):
                full_url = urljoin(day_url, link.get('href'))
                if not (full_url.endswith(('.htm', '.HTM')) and 'index' not in full_url): continue

                print(f"    -> Processing ({seq}): {full_url}")
                release_id = os.path.splitext(os.path.basename(full_url))[0].removesuffix('c')
                
                title, ts, content, attachments, corresponding_url = get_press_release_content(full_url, conn, download_attachments)
                if title and content:
                    release_data = {
                        'url': full_url, 'release_id': release_id, 'date': date.strftime("%Y-%m-%d"),
                        'sequence_in_day': seq, 'language': lang, 'title': title, 'timestamp': ts, 'content': content,
                        'corresponding_url': corresponding_url
                    }
                    insert_data(conn, release_data, attachments)
                    msg = f" and processed {len(attachments)} attachment link(s)"
                    print(f"      -> Saved release{msg} to database.")
                time.sleep(0.5)

            if enable_resume:
                conn.execute("INSERT OR IGNORE INTO processed_pages (page_url) VALUES (?)", (day_url,))
                conn.commit()

        except requests.exceptions.RequestException as e:
            print(f"Error fetching day page {day_url}: {e}")
        time.sleep(1)

def get_db_files_for_range(args):
    """Generates a list of database filenames for the given date range and settings."""
    if not args.separate_db:
        return [f"{args.output}.db"]
    
    db_files = OrderedDict()
    current_date = args.date_from
    while current_date <= args.date_to:
        month_str = current_date.strftime("%Y-%m")
        db_name = f"{args.output}_{month_str}.db"
        if db_name not in db_files:
            db_files[db_name] = None
        current_date += timedelta(days=1)
    return list(db_files.keys())

def download_recorded_attachments(args):
    """Scans DBs for attachment links without data and downloads them."""
    print("--- Starting Download-Only Mode ---")
    db_files = get_db_files_for_range(args)
    
    for db_name in db_files:
        if not os.path.exists(db_name):
            print(f"Database {db_name} not found, skipping.")
            continue
        
        print(f"\nProcessing database: {db_name}")
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        
        cursor.execute("SELECT attachment_url, file_type FROM attachments WHERE base64_data IS NULL")
        pending_attachments = cursor.fetchall()
        
        if not pending_attachments:
            print("  -> No pending attachments to download in this database.")
            conn.close()
            continue
            
        print(f"  -> Found {len(pending_attachments)} pending attachment(s).")
        for url, file_type in pending_attachments:
            allowed_extensions = ['jpg', 'jpeg', 'png', 'gif', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx']
            if file_type in allowed_extensions:
                base64_data = download_and_encode(url)
                if base64_data:
                    try:
                        conn.execute("UPDATE attachments SET base64_data = ? WHERE attachment_url = ?", (base64_data, url))
                        conn.commit()
                        print(f"      -> Successfully downloaded and saved to DB.")
                    except sqlite3.Error as e:
                        print(f"      -> DB Error while updating {url}: {e}")
                        conn.rollback()
                time.sleep(1)
            else:
                print(f"    -> Skipping unsupported file type '{file_type}': {url}")

        conn.close()
    print("\n--- Download-Only Mode Finished ---")

def main():
    """
    Main function to parse arguments and orchestrate the crawling process.
    """
    parser = argparse.ArgumentParser(description="Crawl press releases from info.gov.hk.")
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    def valid_date(s):
        try: return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError: raise argparse.ArgumentTypeError(f"Not a valid date: '{s}'. Expected format: YYYY-MM-DD.")

    parser.add_argument("--date-from", type=valid_date, default=today_str, help="Start date (YYYY-MM-DD).")
    parser.add_argument("--date-to", type=valid_date, default=today_str, help="End date (YYYY-MM-DD).")
    parser.add_argument("--output", type=str, default="press_releases", help="Base name for the output file(s).")
    parser.add_argument("--separate-db", action="store_true", help="Create a separate database file for each month.")
    parser.add_argument("--download-attachments", action="store_true", help="Download attachments during the crawl.")
    parser.add_argument("--resume", action="store_true", help="Skip pages that have already been processed (crawl mode only).")
    parser.add_argument("--download-only", action="store_true", help="Scan DB for links and download pending attachments. Skips crawling.")
    
    args = parser.parse_args()

    if args.date_from > args.date_to:
        parser.error("--date-from cannot be after --date-to.")

    if args.download_only:
        download_recorded_attachments(args)
        return

    # --- Crawl Mode ---
    conn, current_month_str = None, ""
    print(f"--- Starting Crawl Mode from {args.date_from} to {args.date_to} ---")
    print(f"Resume mode: {'Enabled' if args.resume else 'Disabled'}")
    
    current_date = args.date_from
    while current_date <= args.date_to:
        db_name = ""
        if args.separate_db:
            new_month_str = current_date.strftime("%Y-%m")
            if new_month_str != current_month_str:
                if conn: conn.close()
                current_month_str = new_month_str
                db_name = f"{args.output}_{current_month_str}.db"
                conn = setup_database(db_name, args.resume)
        elif conn is None:
            db_name = f"{args.output}.db"
            conn = setup_database(db_name, args.resume)

        print(f"\n--- Processing Date: {current_date.strftime('%Y-%m-%d')} ---")
        crawl_day_page(current_date, conn, args.download_attachments, args.resume)
        current_date += timedelta(days=1)

    if conn: conn.close()
    print(f"\n--- Crawl Mode Finished ---")

if __name__ == "__main__":
    main()
