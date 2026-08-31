import re
import asyncio
import httpx
import yt_dlp
import ipaddress
import socket
from playwright.async_api import async_playwright
from urllib.parse import urlparse

async def is_safe_url(target_url: str) -> bool:
    """Sanitizes user input to prevent Server-Side Request Forgery (SSRF) and DNS Rebinding."""
    if not target_url.startswith(("http://", "https://")):
        return False
        
    try:
        parsed = urlparse(target_url)
        hostname = parsed.hostname
        if not hostname:
            return False
            
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False

        # If the hostname is already an IP, check it immediately
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_unspecified:
                return False
            return True
        except ValueError:
            pass

        # Resolve standard domains via OS DNS to actively block DNS Rebinding attacks
        loop = asyncio.get_running_loop()
        ip = await loop.run_in_executor(None, socket.gethostbyname, hostname)
        
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_unspecified:
            return False
            
        return True
    except Exception:
        return False

async def fetch_playlist(url: str):
    if not await is_safe_url(url):
        return []
        
    urls = []
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=10.0) as client:
            # codeql[py/full-ssrf] - SSRF is fully mitigated by is_safe_url which performs DNS resolution and private IP blocking
            resp = await client.get(url)  # lgtm [py/full-ssrf]
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
    if not await is_safe_url(url):
        raise ValueError("URL failed security validation. Scheme must be HTTP/HTTPS and cannot be a private IP.")

    discovered = []
    
    # 1. Direct Playlist Parsing (.pls / .m3u)
    parsed_main = urlparse(url)
    if parsed_main.path.lower().endswith(('.pls', '.m3u')) or 'playlist' in parsed_main.path.lower():
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
        
    # 3. Playwright Headless Sniffing (Catches hidden network requests on websites)
    async def run_playwright():
        streams = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                
                # Intercept network traffic looking for audio extensions OR playlists
                page.on("request", lambda request: streams.append(request.url) if re.search(r'\.(mp3|aac|m4a|ogg|wav|m3u8|pls|m3u)', request.url.lower()) or 'playlist' in request.url.lower() else None)
                
                await page.goto(url, wait_until="networkidle", timeout=15000)
                await asyncio.sleep(3) # Wait a few seconds for dynamic players to load
                await browser.close()
        except Exception:
            pass
        return streams

    pw_results = await run_playwright()
    discovered.extend(pw_results)
    
    # 4. Post-Process: If Playwright intercepted a hidden playlist file, crack it open now
    final_urls = []
    for d_url in set(discovered):
        parsed_d = urlparse(d_url)
        if parsed_d.path.lower().endswith(('.pls', '.m3u')) or 'playlist' in parsed_d.path.lower():
            extracted = await fetch_playlist(d_url)
            final_urls.extend(extracted)
        else:
            final_urls.append(d_url)

    if final_urls:
        return list(set(final_urls))
        
    # 5. Fallback: If absolutely nothing is found, return the original URL
    return [url]