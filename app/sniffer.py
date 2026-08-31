import re
import asyncio
import httpx
import yt_dlp
import ipaddress
from playwright.async_api import async_playwright
from urllib.parse import urlparse

def is_safe_url(target_url: str) -> bool:
    """Sanitizes user input to prevent Server-Side Request Forgery (SSRF)."""
    # Enforce strict HTTP/HTTPS schemes
    if not target_url.startswith(("http://", "https://")):
        return False
        
    try:
        parsed = urlparse(target_url)
        hostname = parsed.hostname
        if not hostname:
            return False
            
        # Block obvious local SSRF targets
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False

        # Block internal/private network IP address ranges
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_unspecified:
                return False
        except ValueError:
            pass # It is a standard domain name, which is safe to proceed
            
        return True
    except Exception:
        return False

async def fetch_playlist(url: str):
    if not is_safe_url(url):
        return []
        
    urls = []
    try:
        # Fetch the plain text of the .pls or .m3u file safely
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
    if not is_safe_url(url):
        raise ValueError("URL failed security validation. Scheme must be HTTP/HTTPS and cannot be a private IP.")
        
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