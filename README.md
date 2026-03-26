# Reddit Media Downloader

Bulk download images, GIFs, videos, and galleries from any public subreddit or user.

## Setup

1. Install Python 3.8+ if you haven't already.

2. Install the dependency:
   ```
   pip install flask
   ```

3. Run the server:
   ```
   python app.py
   ```

4. Open your browser at: http://localhost:5000

## Usage

- Enter `r/earthporn`, `r/pics`, `u/someuser`, etc.
- Choose sort order (Hot / New / Top / Rising) and post limit
- Click **Fetch media** — all images, GIFs, videos, and gallery slides are extracted
- Filter by type using the pills
- Click cards to select, then **Download selected** or **Download all**

## Notes

- Reddit-hosted videos (`v.redd.it`) are downloaded without audio (audio is on a separate stream)
- Only public subreddits and users work — private/banned ones will return an error
- Downloads trigger one by one with a small delay to avoid browser popup blockers
