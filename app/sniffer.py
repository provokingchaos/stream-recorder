import asyncio
from playwright.async_api import async_playwright
import yt_dlp

async def sniff_stream_url(target_url: str) -> str:
    loop = asyncio.get_running_loop()
    
    def try_ytdlp(url):
        ydl_opts = {'quiet': True, 'skip_download': True, 'extract_flat': False}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                return info.get('url')
            except Exception:
                return None

    resolved = await loop.run_in_executor(None, try_ytdlp, target_url)
    if resolved:
        return resolved

    found_urls = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page()

        def handle_request(request):
            url = request.url
            if any(ext in url.lower() for ext in ['.m3u8', '.mp3', '.aac', 'icecast', 'shoutcast', '/stream']):
                found_urls.append(url)

        page.on("request", handle_request)
        page.on("response", lambda res: handle_request(res.request))

        try:
            await page.goto(target_url, wait_until="networkidle", timeout=25000)
            await page.evaluate("""() => {
                const playBtn = document.querySelector('button[aria-label*="play" i], button[title*="play" i], .play-button, audio, video');
                if (playBtn) playBtn.click();
            }""")
            await asyncio.sleep(6)
        except Exception:
            pass
        finally:
            await browser.close()

    if found_urls:
        return found_urls[0]
    raise ValueError("Could not extract a valid audio stream from the target page.")
