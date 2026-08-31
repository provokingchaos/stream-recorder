import re
import asyncio
import httpx
import yt_dlp
from playwright.async_api import async_playwright
from urllib.parse import urlparse

async def fetch_playlist(url: str):
    urls = []
    try:
        # Fetch the plain text of the .pls or .m3u file
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text
            
            # Parse .pls format (looks for File1=http...)
            if re.search(r'^File\d+=', text, re.IGNORECASE | re.MULTILINE):
                matches = re.findall(r'^File\d+=(.+)$', text, re.MULTILINE | re.IGNORECASE)
                urls.extend([m.strip() for m in matches if m.strip()])
            
            # Parse standard .m3u format (ignores # comment lines)
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        urls.append(line)
    except Exception as e:
        print(f"Playlist extraction error: {e}")
    return urls

async def sniff_stream_url(url: str):
    discovered = []
    
    # 1. Direct Playlist Parsing (.pls / .m3u)
    parsed_url = urlparse(url)
    if parsed_url.path.lower().endswith(('.pls', '.m3u')) or 'playlist' in parsed_url.path.lower():
        playlist_urls = await fetch_playlist(url)
        if playlist_urls:
            return playlist_urls

    # 2. YT-DLP Extraction (Great for YouTube, Twitch, Mixcloud, etc.)
    def extract_ydl():
        ydl_opts = {'quiet': True, 'extract_flat': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if info and 'url' in info:
                    return [info['url']]
                if info and 'entries' in info:
                    return [entry['url'] for entry in info['entries'] if 'url' in entry]
            except Exception:
                pass
        return []
        
    try:
        ydl_results = await asyncio.to_thread(extract_ydl)
        if ydl_results:
            discovered.extend(ydl_results)
    except Exception:
        pass
        
    if discovered:
        return list(set(discovered))
        
    # 3. Playwright Headless Sniffing (Catches hidden network requests on websites)
    async def run_playwright():
        streams = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                
                # Intercept network traffic looking for audio extensions
                page.on("request", lambda request: streams.append(request.url) if re.search(r'\.(mp3|aac|m4a|ogg|wav|m3u8)', request.url.lower()) else None)
                
                await page.goto(url, wait_until="networkidle", timeout=15000)
                await asyncio.sleep(3) # Wait a few seconds for dynamic players to load
                await browser.close()
        except Exception:
            pass
        return streams

    pw_results = await run_playwright()
    discovered.extend(pw_results)
    
    # 4. Fallback: If absolutely nothing is found, return the original URL
    if not discovered:
        discovered.append(url)
        
    return list(set(discovered))