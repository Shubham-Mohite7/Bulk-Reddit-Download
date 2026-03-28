from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import os
import json
import time
import threading
import io
import zipfile
import hashlib
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import requests
import urllib3
import gzip
from datetime import datetime, timedelta
from collections import defaultdict

app = Flask(__name__, static_folder="static")
CORS(app)

# Performance optimizations for Vercel
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 year cache for static files
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False  # Faster JSON responses

# Check if running on Vercel
IS_VERCEL = os.environ.get('VERCEL') == '1'

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}

# Use urllib3 for better connection pooling in downloads
import urllib3
http = urllib3.PoolManager(
    num_pools=30,  # Increased for more concurrent connections
    maxsize=30,
    retries=urllib3.Retry(total=2, backoff_factor=0.1)
)

# Connection pooling for better performance (used by fetch functions)
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
THREAD_POOL = ThreadPoolExecutor(max_workers=20)  # Increased from 10 to 20

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
    
    # Try multiple approaches to avoid blocking
    approaches = [
        # Approach 1: Direct with full headers
        {
            "headers": HEADERS,
            "timeout": 10
        },
        # Approach 2: Minimal headers (fallback)
        {
            "headers": {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            "timeout": 15
        },
        # Approach 3: Reddit app user agent
        {
            "headers": {
                "User-Agent": "Reddit:RedditMediaDownloader:v1.0.0 (by /u/RedditDownloader)"
            },
            "timeout": 20
        }
    ]
    
    for i, approach in enumerate(approaches):
        try:
            response = SESSION.get(url, headers=approach["headers"], timeout=approach["timeout"])
            response.raise_for_status()
            
            # Handle gzip compression
            if response.headers.get('content-encoding') == 'gzip':
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    # If JSON decode fails, decompress manually
                    import gzip
                    content = gzip.decompress(response.content)
                    data = json.loads(content.decode('utf-8'))
            else:
                data = response.json()
            
            # Cache the result
            set_cache(cache_key, data)
            
            return jsonify({"ok": True, "data": data, "cached": False})
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return jsonify({"error": "Subreddit or user not found. Check the spelling and try again."}), 404
            elif e.response.status_code == 403:
                # Try next approach for 403 errors
                if i < len(approaches) - 1:
                    time.sleep(1)  # Brief delay before retry
                    continue
                return jsonify({"error": "Access denied. This subreddit may be private or requires authentication."}), 403
            elif e.response.status_code == 429:
                return jsonify({"error": "Too many requests. Please wait a moment and try again."}), 429
            elif e.response.status_code == 500:
                return jsonify({"error": "Reddit server error. Please try again later."}), 500
            else:
                return jsonify({"error": f"Reddit returned HTTP {e.response.status_code}. Please try again."}), 400
                
        except requests.exceptions.Timeout:
            if i < len(approaches) - 1:
                continue
            return jsonify({"error": "Request timed out. Reddit may be slow. Please try again."}), 408
        except requests.exceptions.ConnectionError:
            if i < len(approaches) - 1:
                continue
            return jsonify({"error": "Network error. Please check your internet connection."}), 500
        except json.JSONDecodeError:
            if i < len(approaches) - 1:
                continue
            return jsonify({"error": "Invalid response from Reddit. Please try again."}), 500
        except Exception as e:
            if i < len(approaches) - 1:
                continue
            return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    
    return jsonify({"error": "All connection attempts failed. Please try again later."}), 500

def fetch_paginated_posts(path, limit):
    """Fetch posts using sequential pagination"""
    all_posts = []
    last_id = None
    total_fetched = 0
    max_requests = (limit // 100) + 2  # Safety buffer
    
    while total_fetched < limit and len(all_posts) < limit:
        current_limit = min(100, limit - total_fetched)
        url = f"https://www.reddit.com/{path}.json?limit={current_limit}&raw_json=1"
        
        if last_id:
            url += f"&after={last_id}"
        
        try:
            cache_key = get_cache_key(url)
            cached_data = get_from_cache(cache_key)
            if cached_data:
                data = cached_data
            else:
                response = SESSION.get(url, timeout=15)
                response.raise_for_status()
                
                # Handle gzip compression
                if response.headers.get('content-encoding') == 'gzip':
                    try:
                        data = response.json()
                    except json.JSONDecodeError:
                        import gzip
                        content = gzip.decompress(response.content)
                        data = json.loads(content.decode('utf-8'))
                else:
                    data = response.json()
                
                set_cache(cache_key, data)
            
            posts = data.get('data', {}).get('children', [])
            if not posts:
                break
            
            # Check for duplicates
            new_posts = []
            seen_ids = set(post['data']['name'] for post in all_posts)
            
            for post in posts:
                if post['data']['name'] not in seen_ids:
                    new_posts.append(post)
                    seen_ids.add(post['data']['name'])
            
            if not new_posts:
                break
            
            all_posts.extend(new_posts)
            total_fetched += len(posts)
            last_id = posts[-1]['data']['name']
            
            # Add delay to avoid rate limiting
            time.sleep(0.2)
            
        except Exception as e:
            if all_posts:
                break
            return jsonify({"error": f"Failed to fetch posts: {str(e)}"}), 500
    
    # Return combined data
    combined_data = {
        'data': {
            'children': all_posts[:limit],
            'after': last_id if all_posts else None
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
    seen_post_ids = set()  # Track seen posts to detect duplicates
    
    while request_count < max_requests:
        url = f"https://www.reddit.com/{path}.json?limit=100&raw_json=1"
        
        if last_id:
            url += f"&after={last_id}"
        
        try:
            response = SESSION.get(url, timeout=15)
            response.raise_for_status()
            
            # Handle gzip compression
            if response.headers.get('content-encoding') == 'gzip':
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    # If JSON decode fails, decompress manually
                    import gzip
                    content = gzip.decompress(response.content)
                    data = json.loads(content.decode('utf-8'))
            else:
                data = response.json()
            
            posts = data.get('data', {}).get('children', [])
            if not posts:
                break  # No more posts available
            
            # Check for duplicates
            new_posts = []
            duplicate_count = 0
            for post in posts:
                post_id = post['data']['name']
                if post_id not in seen_post_ids:
                    seen_post_ids.add(post_id)
                    new_posts.append(post)
                else:
                    duplicate_count += 1
            
            if duplicate_count > 0:
                pass  # Silently skip duplicates
            
            if not new_posts:
                break
            
            all_posts.extend(new_posts)
            total_fetched += len(new_posts)
            request_count += 1
            last_id = new_posts[-1]['data']['name']
            
            # Safety check to prevent infinite loops - max 2500 posts
            if total_fetched >= 2500:
                break
            
            # Add delay to avoid rate limiting
            if request_count > 1:
                time.sleep(0.2)  # Reduced from 0.5 to 0.2 for faster processing
                
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                # Rate limited - wait and retry once
                time.sleep(2)
                continue
            elif e.response.status_code in [403, 404]:
                return jsonify({"error": f"Access denied or not found. The subreddit may be private or doesn't exist."}), e.response.status_code
            else:
                # For other errors, return what we have so far
                if all_posts:
                    break
                return jsonify({"error": f"HTTP {e.response.status_code}: {str(e)}"}), e.response.status_code
                
        except requests.exceptions.Timeout:
            # Timeout - return what we have if anything
            if all_posts:
                break
            return jsonify({"error": "Request timed out. Reddit may be slow. Please try again."}), 408
        except requests.exceptions.ConnectionError:
            # Connection error - return what we have if anything
            if all_posts:
                break
            return jsonify({"error": "Network error. Please check your internet connection."}), 500
                
        except json.JSONDecodeError:
            if all_posts:
                break
            return jsonify({"error": "Invalid response from Reddit. Please try again."}), 500
            
        except Exception as e:
            if all_posts:
                break
            return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    
    # Return combined data
    combined_data = {
        'data': {
            'children': all_posts,
            'after': last_id if all_posts else None
        }
    }
    
    return jsonify({"ok": True, "data": combined_data})

# Simple progress tracking for Vercel (no SocketIO)
download_progress = {}
progress_lock = threading.Lock()

def update_progress(session_id, completed, total, current_file=None):
    """Update download progress (simplified for Vercel)"""
    with progress_lock:
        progress_data = {
            'completed': completed,
            'total': total,
            'percentage': int((completed / total) * 100) if total > 0 else 0,
            'current_file': current_file,
            'remaining': total - completed,
            'timestamp': time.time()
        }
        download_progress[session_id] = progress_data

@app.route("/api/progress/<session_id>", methods=["GET"])
def get_progress(session_id):
    """Get download progress for polling (Vercel compatible)"""
    with progress_lock:
        if session_id in download_progress:
            # Clean up old progress (older than 5 minutes)
            if time.time() - download_progress[session_id]['timestamp'] > 300:
                del download_progress[session_id]
                return jsonify({"error": "Progress expired"}), 404
            return jsonify(download_progress[session_id])
        return jsonify({"error": "Progress not found"}), 404

@app.route("/api/browse-directories", methods=["POST"])
def browse_directories():
    """Browse directories and validate custom destination path (Vercel compatible)"""
    try:
        data = request.get_json()
        path = data.get("path", "")
        
        if IS_VERCEL:
            # On Vercel, return only virtual/common directories
            return jsonify({
                "success": True,
                "directories": [
                    {"name": "Default", "path": ""},
                    {"name": "Downloads", "path": "~/Downloads"},
                    {"name": "Desktop", "path": "~/Desktop"}
                ]
            })
        
        if not path:
            # Return common directories
            home = os.path.expanduser("~")
            desktop = os.path.join(home, "Desktop")
            documents = os.path.join(home, "Documents")
            downloads = os.path.join(home, "Downloads")
            current = os.getcwd()
            
            return jsonify({
                "success": True,
                "directories": [
                    {"name": "Desktop", "path": desktop},
                    {"name": "Documents", "path": documents},
                    {"name": "Downloads", "path": downloads},
                    {"name": "Project Folder", "path": current}
                ]
            })
        
        # Validate and list directory contents
        if path.startswith("~"):
            path = os.path.expanduser(path)
        
        if not os.path.exists(path):
            return jsonify({"error": "Directory does not exist"}), 400
        
        if not os.path.isdir(path):
            return jsonify({"error": "Path is not a directory"}), 400
        
        # List directories in the path
        try:
            items = []
            for item in os.listdir(path):
                item_path = os.path.join(path, item)
                if os.path.isdir(item_path) and not item.startswith('.'):
                    items.append({"name": item, "path": item_path})
            
            return jsonify({
                "success": True,
                "current_path": path,
                "directories": items
            })
            
        except PermissionError:
            return jsonify({"error": "Permission denied"}), 403
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download", methods=["POST"])
def download_media():
    # Check rate limiting (more lenient for downloads)
    if not check_rate_limit():
        return jsonify({"error": "Rate limit exceeded. Please wait a moment before making more requests."}), 429
    
    try:
        data = request.get_json()
        items = data.get("items", [])
        custom_destination = data.get("destination", "")  # Get custom destination if provided
        session_id = request.remote_addr or str(time.time())  # Use IP or timestamp as session identifier
        
        if not items:
            return jsonify({"error": "No items to download"}), 400
        
        # Limit download size for performance (smaller on Vercel)
        max_items = 500 if IS_VERCEL else 2500
        if len(items) > max_items:
            return jsonify({"error": f"Too many items requested. Maximum {max_items} items per download."}), 400
        
        if IS_VERCEL:
            # On Vercel, return ZIP file (can't save to filesystem)
            return download_as_zip(items, session_id)
        else:
            return download_individual(items, session_id, custom_destination)
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def download_as_zip(items, session_id):
    """Download files concurrently and return as a ZIP archive (Vercel compatible)"""
    try:
        zip_buffer = io.BytesIO()
        downloaded_count = 0
        failed_count = 0
        
        # Initialize progress
        update_progress(session_id, 0, len(items))
        
        def download_single_file(item_data):
            i, item = item_data
            try:
                url = item["url"]
                
                # Validate URL before downloading
                if not url or not url.startswith(('http://', 'https://')):
                    return None, None, False
                
                post_id = item.get("postId", f"post_{int(time.time())}")
                item_type = item.get("type", "image")
                
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
                
                filename = f"reddit_{post_id}_{i + 1}{ext}"
                
                # Retry logic for better reliability
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        # Use urllib3 for better connection pooling
                        response = http.request('GET', url, timeout=10, retries=False)
                        content = response.data
                        content_length = len(content)
                        
                        # Validate content
                        if content_length < 100:  # Less than 100 bytes is likely an error
                            return None, None, False
                        
                        # Update progress
                        downloaded_count += 1
                        update_progress(session_id, downloaded_count, len(items), filename)
                        
                        return filename, content, True
                        
                    except Exception as retry_e:
                        if attempt < max_retries - 1:
                            time.sleep(0.5)
                            continue
                        else:
                            raise retry_e
                
            except Exception as e:
                # Still update progress even on failure
                downloaded_count += 1
                update_progress(session_id, downloaded_count, len(items), None)
                return None, None, False
        
        max_workers = min(10, len(items))  # Reduced for Vercel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(download_single_file, (i, item)) for i, item in enumerate(items)]
            
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zip_file:
                for future in as_completed(futures):
                    filename, content, success = future.result()
                    if success and filename and content:
                        zip_file.writestr(filename, content)
                    else:
                        failed_count += 1
        
        # Final progress update
        update_progress(session_id, len(items), len(items))
        
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

def download_individual(items, session_id, custom_destination=""):
    """Download files concurrently to custom destination folder with progress tracking"""
    try:
        # Determine download directory
        if custom_destination and custom_destination.strip():
            # Use custom destination (expand user path if needed)
            if custom_destination.startswith("~"):
                downloads_dir = os.path.expanduser(custom_destination)
            else:
                downloads_dir = os.path.abspath(custom_destination)
        else:
            # Default to Downloads folder
            downloads_dir = os.path.join(os.getcwd(), "Downloads")
        
        # Create directory if it doesn't exist
        os.makedirs(downloads_dir, exist_ok=True)
        
        downloaded_files = []
        failed_count = 0
        completed_count = 0
        
        # Initialize progress
        update_progress(session_id, 0, len(items))
        
        def download_single_file(item_data):
            nonlocal completed_count
            i, item = item_data
            try:
                url = item["url"]
                
                # Validate URL before downloading
                if not url or not url.startswith(('http://', 'https://')):
                    return None, False
                
                post_id = item.get("postId", f"post_{int(time.time())}")
                item_type = item.get("type", "image")
                
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
                
                filename = f"reddit_{post_id}_{i + 1}{ext}"
                filepath = os.path.join(downloads_dir, filename)
                
                # Retry logic for better reliability
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        # Use urllib3 for better connection pooling
                        response = http.request('GET', url, timeout=10, retries=False)
                        content = response.data
                        content_length = len(content)
                        
                        # Validate content
                        if content_length < 100:  # Less than 100 bytes is likely an error
                            return None, False
                        
                        # Write file to Downloads folder
                        with open(filepath, 'wb') as f:
                            f.write(content)
                        
                        # Update progress
                        completed_count += 1
                        update_progress(session_id, completed_count, len(items), filename)
                        
                        return filename, True
                        
                    except Exception as retry_e:
                        if attempt < max_retries - 1:
                            time.sleep(0.5)  # Reduced from 1 to 0.5 seconds for faster retries
                            continue
                        else:
                            raise retry_e
                
            except Exception as e:
                # Still update progress even on failure
                completed_count += 1
                update_progress(session_id, completed_count, len(items), None)
                return None, False
        
        max_workers = min(30, len(items))  # Increased from 15 to 30 for faster downloads
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(download_single_file, (i, item)) for i, item in enumerate(items)]
            
            for future in as_completed(futures):
                filename, success = future.result()
                if success and filename:
                    downloaded_files.append(filename)
                else:
                    failed_count += 1
        
        # Final progress update
        update_progress(session_id, len(items), len(items))
        
        if len(downloaded_files) == 0:
            return jsonify({"error": "Failed to download any files. Please check your internet connection and try again."}), 500
        
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
    port = 5001  # Use different port to avoid conflicts
    print(f"\n  Reddit Media Downloader running at http://localhost:{port}\n")
    if IS_VERCEL:
        app.run(debug=False, port=port)
    else:
        app.run(debug=False, port=port)
