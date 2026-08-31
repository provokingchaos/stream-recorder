import re
import os
import sys
import asyncio
import urllib.parse
from playwright.async_api import async_playwright
import yt_dlp
import httpx

class QuietLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass

NON_AUDIO_EXTENSIONS = (
    '.ttf', '.woff', '.woff2', '.eot', '.otf',
    '.html', '.htm', '.js', '.css', '.json',
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.ts'
)

def is_valid_audio_candidate(url: str) -> bool:
    clean_url = url.split('?')[0].lower()
    if clean_url.endswith(NON_AUDIO_EXTENSIONS): return False
    if any(ignore in clean_url for ignore in ['google-analytics', 'googlesyndication', 'doubleclick', 'hotjar', 'facebook']): return False
    return True

async def resolve_live365(station_id: str) -> str:
    try:
        api_url = f"https://api.live365.com/station/{station_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(api_url)
            if res.status_code == 200:
                data = res.json()
                for key in ["stream-url", "stream_url", "direct-stream-url"]:
                    if key in data and data[key]: return data[key]
    except Exception: pass
    return None

async def _sniff_logic(target_url: str) -> list:
    loop = asyncio.get_running_loop()
    found_media = set()

    # 1. Live365 direct URL match
    parsed = urllib.parse.urlparse(target_url)
    if parsed.netloc == "live365.com" or parsed.netloc.endswith(".live365.com"):
        params = urllib.parse.parse_qs(parsed.query)
        if "station" in params:
            live365_stream = await resolve_live365(params["station"][0])
            if live365_stream: found_media.add(live365_stream)

    # 2. yt-dlp aggregation
    def try_ytdlp(url):
        ydl_opts = {'logger': QuietLogger(), 'quiet': True, 'skip_download': True, 'extract_flat': False, 'no_warnings': True, 'ignoreerrors': True}
        urls = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and 'url' in info and is_valid_audio_candidate(info['url']): urls.append(info['url'])
                if info and 'entries' in info:
                    for e in info['entries']:
                        if e.get('url') and is_valid_audio_candidate(e['url']): urls.append(e['url'])
        except Exception: pass
        return urls

    try:
        resolved_list = await loop.run_in_executor(None, try_ytdlp, target_url)
        if resolved_list:
            for r in resolved_list: found_media.add(r)
    except Exception: pass

    # 3. Playwright browser aggregation
    stream_patterns = re.compile(
        r'(\.m3u8|\.mp3|\.aac|\.ogg|\.opus|/stream|/live|/listen|/icecast|/shoutcast|/audio|/hls|cdnstream\.com)',
        re.IGNORECASE
    )

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--autoplay-policy=no-user-gesture-required", "--mute-audio"]
        }

        # Smarter fallback: works natively on macOS and inside Debian Docker containers
        exec_path = None
        for candidate in [
            "/usr/bin/chromium", 
            "/usr/bin/chromium-browser", 
            "/usr/bin/google-chrome",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        ]:
            if os.path.exists(candidate):
                exec_path = candidate
                break
        
        if exec_path:
            launch_kwargs["executable_path"] = exec_path

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 800}
        )

        def handle_request(request):
            url = request.url
            if not is_valid_audio_candidate(url): return
            if request.resource_type == "media" or stream_patterns.search(url):
                found_media.add(url)

        async def inspect_response(response):
            try:
                url = response.url
                if not is_valid_audio_candidate(url): return
                content_type = response.headers.get("content-type", "").lower()
                if any(m in content_type for m in ["audio/", "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/ogg"]):
                    found_media.add(url)
            except Exception: pass

        async def execute_interactions(target_page):
            try:
                station_ids = await target_page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('iframe'))
                        .map(f => f.src.match(/station=([a-zA-Z0-9_\\-]+)/))
                        .filter(m => m).map(m => m[1]);
                }""")
                for sid in station_ids:
                    l365 = await resolve_live365(sid)
                    if l365: found_media.add(l365)
            except Exception: pass

            try:
                iframes = await target_page.locator("iframe").all()
                for frame_loc in iframes:
                    try:
                        box = await frame_loc.evaluate("""el => {
                            const rect = el.getBoundingClientRect();
                            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
                        }""")
                        if box and box["width"] > 0:
                            await target_page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            await asyncio.sleep(0.5)
                    except Exception: pass
            except Exception: pass

            click_js = """() => {
                const selectors = ['button:not([disabled])', '[aria-label*="play" i]', '[title*="play" i]', '.play-button', '.play', 'audio', 'video'];
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => {
                        try { el.click(); } catch(e){}
                        try { if (el.play) el.play(); } catch(e){}
                    });
                });
            }"""
            try: await target_page.evaluate(click_js)
            except Exception: pass
            for frame in target_page.frames:
                try: await frame.evaluate(click_js)
                except Exception: pass

        try:
            page = await context.new_page()
            page.on("request", handle_request)
            page.on("response", inspect_response)

            await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)
            await execute_interactions(page)
            
            # Wait 5 seconds to gather network payloads, do not exit early
            await asyncio.sleep(5)

            iframe_srcs = await page.evaluate("Array.from(document.querySelectorAll('iframe')).map(f => f.src).filter(Boolean)")
            for src in iframe_srcs:
                if "javascript:" in src or "google" in src: continue
                try:
                    widget_page = await context.new_page()
                    widget_page.on("request", handle_request)
                    widget_page.on("response", inspect_response)
                    await widget_page.goto(src, wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(2)
                    vp = widget_page.viewport_size
                    await widget_page.mouse.click(vp["width"] / 2, vp["height"] / 2)
                    await execute_interactions(widget_page)
                    await asyncio.sleep(4)
                    await widget_page.close()
                except Exception: pass

        except Exception as e:
            print(f"[Sniffer Error] {e}", file=sys.stderr, flush=True)
        finally:
            await browser.close()

    if found_media:
        return list(found_media)
    
    raise ValueError("Could not resolve any audio streams. The site may be heavily protected.")

async def sniff_stream_url(target_url: str) -> list:
    try:
        return await asyncio.wait_for(_sniff_logic(target_url), timeout=50.0)
    except asyncio.TimeoutError:
        raise ValueError("Extraction timed out. The page took too long.")