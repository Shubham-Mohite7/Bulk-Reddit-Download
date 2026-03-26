from flask import Flask, jsonify, request, send_from_directory, send_file, after_this_request
import urllib.request
import urllib.error
import json
import os
import requests
from urllib.parse import urlparse
import time
import zipfile
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import hashlib
import gzip
from datetime import datetime, timedelta
from collections import defaultdict
import weakref

app = Flask(__name__, static_folder="static")

# Performance optimizations
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 year cache for static files
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False  # Faster JSON responses

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Connection pooling for better performance
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
# Configure connection pool
adapter = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=20,
    max_retries=3,
    pool_block=False
)
SESSION.mount('http://', adapter)
SESSION.mount('https://', adapter)

# Simple in-memory cache with TTL
cache = {}
cache_lock = threading.Lock()

def get_cache_key(url):
    """Generate cache key from URL"""
    return hashlib.md5(url.encode()).hexdigest()

def get_from_cache(cache_key, ttl_minutes=5):
    """Get data from cache if not expired"""
    with cache_lock:
        if cache_key in cache:
            data, timestamp = cache[cache_key]
            if datetime.now() - timestamp < timedelta(minutes=ttl_minutes):
                return data
            else:
                del cache[cache_key]
    return None

def set_cache(cache_key, data):
    """Set data in cache with timestamp"""
    with cache_lock:
        cache[cache_key] = (data, datetime.now())
        # Keep cache size manageable
        if len(cache) > 100:
            oldest_key = min(cache.keys(), key=lambda k: cache[k][1])
            del cache[oldest_key]

# Thread pool for concurrent operations
THREAD_POOL = ThreadPoolExecutor(max_workers=10)

# Rate limiting for web deployment
rate_limits = defaultdict(int)
rate_limit_window = 60  # 1 minute window
max_requests_per_minute = 30  # Per IP
last_cleanup_time = 0

def get_client_ip():
    """Get client IP for rate limiting"""
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0].split(',')[0].strip()
    return request.remote_addr or 'unknown'

def check_rate_limit():
    """Check if client has exceeded rate limit"""
    global last_cleanup_time
    client_ip = get_client_ip()
    current_time = time.time()
    
    # Clean old entries
    if current_time - last_cleanup_time > rate_limit_window:
        keys_to_remove = [k for k, v in rate_limits.items() 
                        if isinstance(v, float) and current_time - v > rate_limit_window]
        for k in keys_to_remove:
            del rate_limits[k]
        last_cleanup_time = current_time
    
    # Check current requests
    request_count = 0
    for key, value in rate_limits.items():
        if key.startswith(f"{client_ip}_"):
            if current_time - value < rate_limit_window:
                request_count += 1
    
    if request_count >= max_requests_per_minute:
        return False
    
    # Record this request
    rate_limits[f"{client_ip}_{current_time}"] = current_time
    return True

# Response compression
def compress_response(response):
    """Compress JSON responses if client supports gzip"""
    if request.headers.get('Accept-Encoding', '').find('gzip') != -1:
        if response.mimetype == 'application/json':
            data = response.get_data()
            compressed = gzip.compress(data)
            response.set_data(compressed)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = len(compressed)
    return response

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/fetch")
def fetch_reddit():
    # Check rate limiting
    if not check_rate_limit():
        return jsonify({"error": "Rate limit exceeded. Please wait a moment before making more requests."}), 429
    
    query = request.args.get("q", "").strip()
    sort  = request.args.get("sort", "hot")
    limit = request.args.get("limit", "25")

    if not query:
        return jsonify({"error": "Missing query parameter"}), 400

    cleaned = query.lstrip("/")
    if cleaned.lower().startswith("r/"):
        path = f"{cleaned.lower()}/{sort}"
    elif cleaned.lower().startswith("u/") or cleaned.lower().startswith("user/"):
        uname = cleaned.split("/", 1)[1]
        path = f"user/{uname}/submitted/{sort}"
    else:
        path = f"r/{cleaned}/{sort}"

    # Handle lifetime/large requests with pagination
    if limit == "lifetime":
        result = fetch_all_posts(path)
    else:
        limit_int = int(limit)
        # Reddit API max is 100 per request, so for >100 we need pagination
        if limit_int <= 100:
            url = f"https://www.reddit.com/{path}.json?limit={limit_int}&raw_json=1"
            result = fetch_single_url(url)
        else:
            result = fetch_paginated_posts(path, limit_int)
    
    # Apply compression if it's a JSON response
    if isinstance(result, tuple) and len(result) == 2:
        response, status_code = result
        if hasattr(response, 'get_json'):
            response = compress_response(response)
    
    return result

def fetch_single_url(url):
    cache_key = get_cache_key(url)
    
    # Try cache first
    cached_data = get_from_cache(cache_key)
    if cached_data:
        return jsonify({"ok": True, "data": cached_data, "cached": True})
    
    try:
        response = SESSION.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Cache the result
        set_cache(cache_key, data)
        
        return jsonify({"ok": True, "data": data, "cached": False})
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return jsonify({"error": "Subreddit or user not found. Check the spelling and try again."}), 404
        elif e.response.status_code == 403:
            return jsonify({"error": "Access denied. This subreddit may be private or requires authentication."}), 403
        elif e.response.status_code == 429:
            return jsonify({"error": "Too many requests. Please wait a moment and try again."}), 429
        elif e.response.status_code == 500:
            return jsonify({"error": "Reddit server error. Please try again later."}), 500
        else:
            return jsonify({"error": f"Reddit returned HTTP {e.response.status_code}. Please try again."}), 400
    except requests.exceptions.Timeout:
        return jsonify({"error": "Request timed out. Reddit may be slow. Please try again."}), 408
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Network error. Please check your internet connection."}), 500
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid response from Reddit. Please try again."}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

def fetch_paginated_posts(path, limit):
    """Fetch posts using optimized pagination with concurrent requests"""
    all_posts = []
    last_id = None
    remaining = limit
    request_count = 0
    max_requests = (limit // 100) + 2  # Safety buffer
    
    # Calculate how many requests we need
    urls_to_fetch = []
    temp_last_id = None
    
    while remaining > 0 and request_count < max_requests:
        current_limit = min(remaining, 100)
        url = f"https://www.reddit.com/{path}.json?limit={current_limit}&raw_json=1"
        
        if temp_last_id:
            url += f"&after={temp_last_id}"
        
        urls_to_fetch.append(url)
        remaining -= current_limit
        request_count += 1
        temp_last_id = f"t3_{request_count * 100}"  # Estimate next ID
    
    # Fetch URLs concurrently (but with some delay to avoid rate limiting)
    def fetch_url(url):
        try:
            cache_key = get_cache_key(url)
            cached_data = get_from_cache(cache_key)
            if cached_data:
                return cached_data, True
            
            response = SESSION.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Cache the result
            set_cache(cache_key, data)
            
            return data, False
            
        except Exception as e:
            print(f"Error fetching {url}: {str(e)}")
            return None, False
    
    # Process URLs with controlled concurrency
    batch_size = 3  # Fetch 3 URLs concurrently to avoid rate limiting
    for i in range(0, len(urls_to_fetch), batch_size):
        batch = urls_to_fetch[i:i + batch_size]
        
        futures = {THREAD_POOL.submit(fetch_url, url): url for url in batch}
        
        for future in as_completed(futures):
            data, from_cache = future.result()
            if data and 'data' in data:
                posts = data.get('data', {}).get('children', [])
                if posts:
                    all_posts.extend(posts)
                    last_id = posts[-1]['data']['name']
            
            # Small delay between batches to be respectful to Reddit
            if i + batch_size < len(urls_to_fetch):
                time.sleep(0.3)
    
    # Remove duplicates and limit to requested amount
    seen_ids = set()
    unique_posts = []
    for post in all_posts:
        post_id = post['data']['name']
        if post_id not in seen_ids:
            seen_ids.add(post_id)
            unique_posts.append(post)
            if len(unique_posts) >= limit:
                break
    
    # Return combined data
    combined_data = {
        'data': {
            'children': unique_posts[:limit],
            'after': last_id
        }
    }
    
    return jsonify({"ok": True, "data": combined_data})

def fetch_all_posts(path):
    """Fetch all posts from a subreddit/user (lifetime)"""
    all_posts = []
    last_id = None
    total_fetched = 0
    request_count = 0
    max_requests = 25  # 25 requests * 100 posts = 2500 max posts
    
    while request_count < max_requests:
        url = f"https://www.reddit.com/{path}.json?limit=100&raw_json=1"
        
        if last_id:
            url += f"&after={last_id}"
        
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            
            posts = data.get('data', {}).get('children', [])
            if not posts:
                print(f"No more posts found after {request_count} requests, {total_fetched} total posts")
                break  # No more posts available
            
            all_posts.extend(posts)
            total_fetched += len(posts)
            request_count += 1
            last_id = posts[-1]['data']['name']
            
            print(f"Request {request_count}: Fetched {len(posts)} posts, total: {total_fetched}")
            
            # Safety check to prevent infinite loops - max 2500 posts
            if total_fetched >= 2500:
                print(f"Reached maximum limit of 2,500 posts")
                break
            
            # If we got fewer than 100 posts, we've reached the end
            if len(posts) < 100:
                print(f"Reached end - got only {len(posts)} posts in last request")
                break
            
            # Add delay to avoid rate limiting
            if request_count > 1:
                time.sleep(0.5)
                
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rate limited - wait and retry once
                print(f"Rate limited, waiting 2 seconds...")
                time.sleep(2)
                continue
            elif e.code in [403, 404]:
                return jsonify({"error": f"Access denied or not found. The subreddit may be private or doesn't exist."}), e.code
            else:
                # For other errors, return what we have so far
                if all_posts:
                    print(f"HTTP error {e.code}, returning {len(all_posts)} posts fetched so far")
                    break
                return jsonify({"error": f"HTTP {e.code}: {str(e)}"}), e.code
                
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                # Timeout - return what we have if anything
                if all_posts:
                    print(f"Timeout, returning {len(all_posts)} posts fetched so far")
                    break
                return jsonify({"error": "Request timed out. Reddit may be slow. Please try again."}), 408
            else:
                if all_posts:
                    print(f"Network error, returning {len(all_posts)} posts fetched so far")
                    break
                return jsonify({"error": "Network error. Please check your internet connection."}), 500
                
        except json.JSONDecodeError:
            if all_posts:
                print(f"JSON decode error, returning {len(all_posts)} posts fetched so far")
                break
            return jsonify({"error": "Invalid response from Reddit. Please try again."}), 500
            
        except Exception as e:
            if all_posts:
                print(f"Unexpected error, returning {len(all_posts)} posts fetched so far: {str(e)}")
                break
            return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    
    print(f"Final result: {len(all_posts)} posts fetched in {request_count} requests")
    
    # Return combined data
    combined_data = {
        'data': {
            'children': all_posts,
            'after': last_id
        }
    }
    
    return jsonify({"ok": True, "data": combined_data})

@app.route("/api/download", methods=["POST"])
def download_media():
    # Check rate limiting (more lenient for downloads)
    if not check_rate_limit():
        return jsonify({"error": "Rate limit exceeded. Please wait a moment before making more requests."}), 429
    
    try:
        data = request.get_json()
        items = data.get("items", [])
        zip_mode = data.get("zipMode", True)
        
        if not items:
            return jsonify({"error": "No items to download"}), 400
        
        # Limit download size for performance
        if len(items) > 500:
            return jsonify({"error": "Too many items requested. Maximum 500 items per download."}), 400
        
        if zip_mode:
            return download_as_zip(items)
        else:
            return download_individual(items)
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def download_as_zip(items):
    """Download files concurrently and return as a ZIP archive"""
    try:
        zip_buffer = io.BytesIO()
        downloaded_count = 0
        failed_count = 0
        
        def download_single_file(item_data):
            """Download a single file"""
            i, item = item_data
            try:
                url = item["url"]
                post_id = item.get("postId", f"post_{int(time.time())}")
                item_type = item.get("type", "image")
                
                # Get file extension
                parsed_url = urlparse(url)
                original_ext = os.path.splitext(parsed_url.path)[1]
                
                if not original_ext:
                    if item_type == "video":
                        ext = ".mp4"
                    elif item_type == "gif":
                        ext = ".gif"
                    else:
                        ext = ".jpg"
                else:
                    ext = original_ext
                
                # Generate filename
                filename = f"reddit_{post_id}_{i + 1}{ext}"
                
                # Download the file with optimized session
                response = SESSION.get(url, stream=True, timeout=15)
                response.raise_for_status()
                
                # Read content efficiently
                content = response.content
                return filename, content, True
                
            except Exception as e:
                print(f"Error downloading {item.get('url', 'unknown')}: {str(e)}")
                return None, None, False
        
        # Download files concurrently (limit concurrency to avoid overwhelming servers)
        max_workers = min(8, len(items))  # Up to 8 concurrent downloads
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(download_single_file, (i, item)) for i, item in enumerate(items)]
            
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
                for future in as_completed(futures):
                    filename, content, success = future.result()
                    if success and filename and content:
                        zip_file.writestr(filename, content)
                        downloaded_count += 1
                    else:
                        failed_count += 1
        
        if downloaded_count == 0:
            return jsonify({"error": "Failed to download any files. Please check your internet connection and try again."}), 500
        
        zip_buffer.seek(0)
        
        # Generate timestamp for filename
        timestamp = int(time.time())
        zip_filename = f"reddit_media_{timestamp}.zip"
        
        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name=zip_filename,
            mimetype='application/zip'
        )
        
    except Exception as e:
        return jsonify({"error": f"Failed to create ZIP file: {str(e)}"}), 500

def download_individual(items):
    """Download files to server storage (for local deployment)"""
    try:
        # Create downloads directory if it doesn't exist
        downloads_dir = os.path.join(os.getcwd(), "downloads")
        os.makedirs(downloads_dir, exist_ok=True)
        
        downloaded_files = []
        
        for i, item in enumerate(items):
            try:
                url = item["url"]
                post_id = item.get("postId", f"post_{int(time.time())}")
                item_type = item.get("type", "image")
                
                # Get file extension from URL or default based on type
                parsed_url = urlparse(url)
                original_ext = os.path.splitext(parsed_url.path)[1]
                
                if not original_ext:
                    if item_type == "video":
                        ext = ".mp4"
                    elif item_type == "gif":
                        ext = ".gif"
                    else:
                        ext = ".jpg"
                else:
                    ext = original_ext
                
                # Generate filename
                filename = f"reddit_{post_id}_{i + 1}{ext}"
                filepath = os.path.join(downloads_dir, filename)
                
                # Download the file
                response = requests.get(url, headers=HEADERS, stream=True, timeout=30)
                response.raise_for_status()
                
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                downloaded_files.append(filename)
                
            except Exception as e:
                print(f"Error downloading {url}: {str(e)}")
                continue
        
        return jsonify({
            "success": True,
            "downloaded": len(downloaded_files),
            "files": downloaded_files,
            "directory": downloads_dir
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    print("\n  Reddit Media Downloader running at http://localhost:5000\n")
    app.run(debug=False, port=5000)
