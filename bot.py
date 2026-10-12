python
import asyncio, os, json, random, string, re, imaplib, email, logging, time
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import camoufox.async_api as camoufox

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
BASE_GMAIL = os.getenv("BASE_GMAIL")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")
PROXY_FILE = os.getenv("PROXY_FILE", "proxies.txt")
STATE_FILE = "bot_state.json"
LOG_FILE = "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(name)

@dataclass
class Proxy:
    host: str
    port: int
    user: Optional[str] = None
    password: Optional[str] = None
    scheme: str = "http"
    country: str = "US"
    healthy: bool = True
    last_used: Optional[str] = None
    success_count: int = 0
    fail_count: int = 0
    
    @property
    def url(self) -> str:
        auth = f"{self.user}:{self.password}@" if self.user else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"
    
    def to_dict(self): return asdict(self)
    @classmethod
    def from_dict(cls, d): return cls(**d)

@dataclass
class Account:
    username: str
    email: str
    password: str
    proxy: str
    created_at: str
    status: str = "created"
    warmed_at: Optional[str] = None
    
    def to_dict(self): return asdict(self)
    @classmethod
    def from_dict(cls, d): return cls(**d)

@dataclass
class BotState:
    running: bool = False
    current_task: Optional[dict] = None
    stats: dict = None
    
    def __post_init__(self):
        if self.stats is None:
            self.stats = {"total": 0, "success": 0, "failed": 0, "banned": 0, "warmed": 0}

state = BotState()
proxies: List[Proxy] = []
accounts: List[Account] = []
task_lock = asyncio.Lock()
rate_limiter = defaultdict(list)
FAILURE_TYPES = {
    "datadome_timeout": "DataDome challenge didn't clear",
    "email_code_timeout": "No verification email",
    "captcha": "hCaptcha/recaptcha triggered",
    "proxy_blocked": "Proxy rejected by GitHub",
    "rate_limit": "Too many requests",
    "form_error": "Invalid form submission",
    "unknown": "Unclassified"
}

COUNTRY_LOCALE = {
    "US": ("en-US", "America/Los_Angeles"),
    "GB": ("en-GB", "Europe/London"),
    "DE": ("de-DE", "Europe/Berlin"),
    "JP": ("ja-JP", "Asia/Tokyo"),
    "SG": ("en-SG", "Asia/Singapore"),
    "CA": ("en-CA", "America/Toronto"),
    "AU": ("en-AU", "Australia/Sydney"),
    "FR": ("fr-FR", "Europe/Paris"),
    "NL": ("nl-NL", "Europe/Amsterdam"),
}

def load_state():
    global state, proxies, accounts
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            data = json.load(f)
            state = BotState(**data.get("state", {}))
            proxies = [Proxy.from_dict(p) for p in data.get("proxies", [])]
            accounts = [Account.from_dict(a) for a in data.get("accounts", [])]
    load_proxies_from_file()

def save_state():
    data = {
        "state": asdict(state),
        "proxies": [p.to_dict() for p in proxies],
        "accounts": [a.to_dict() for a in accounts]
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_proxies_from_file():
    global proxies
    if not Path(PROXY_FILE).exists(): return
    with open(PROXY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            try:
                if "://" in line:
                    scheme, rest = line.split("://", 1)
                else:
                    scheme, rest = "http", line
                if "@" in rest:
                    auth, addr = rest.split("@", 1)
                    user, password = auth.split(":", 1)
                else:
                    user = password = None
                    addr = rest
                host, port = addr.rsplit(":", 1)
                proxy = Proxy(host, int(port), user, password, scheme)
                if not any(p.host == host and p.port == port for p in proxies):
                    proxies.append(proxy)
            except: pass
    save_state()

def gmail_variants(base: str, count: int) -> List[str]:
    local, domain = base.split("@")
    n = len(local)
    max_variants = min(2**(n-1), count)
    variants = []
    for i in range(1, max_variants + 1):
        mask = bin(i)[2:].zfill(n-1)
        variant = local[0]
        for bit, ch in zip(mask, local[1:]):
            variant += ("." if bit == "1" else "") + ch
        variants.append(f"{variant}@{domain}")
    while len(variants) < count:
        dots = ''.join(random.choice(['.', '']) for _ in range(n-1))
        variant = ''.join(c + (dots[i] if i < len(dots) else '') for i, c in enumerate(local))
        variants.append(f"{variant}@{domain}")
    return variants[:count]

def proxy_locale(proxy: Proxy) -> tuple:
    return COUNTRY_LOCALE.get(proxy.country, ("en-US", "America/Los_Angeles"))

async def rate_limit_wait(proxy_url: str, min_interval=30, max_per_hour=5):
    now = time.time()
    rate_limiter[proxy_url] = [t for t in rate_limiter[proxy_url] if now - t < 3600]
    if len(rate_limiter[proxy_url]) >= max_per_hour:
        oldest = rate_limiter[proxy_url][0]
        wait = 3600 - (now - oldest) + 10
        log.warning(f"Rate limit: waiting {wait:.0f}s for {proxy_url}")
        await asyncio.sleep(wait)
    rate_limiter[proxy_url].append(now)

async def human_delay(min_ms=50, max_ms=200):
    await asyncio.sleep(random.uniform(min_ms/1000, max_ms/1000))

async def human_type(page, selector: str, text: str):
    await page.click(selector)
    await human_delay()
    for char in text:
        await page.keyboard.type(char, delay=random.randint(50, 200))
    await human_delay()

async def human_mouse_move(page, target_selector: str = None):
    viewport = page.viewport_size
    if not viewport: return
    start_x, start_y = random.randint(0, viewport["width"]), random.randint(0, viewport["height"])
    if target_selector:
        box = await page.locator(target_selector).bounding_box()
        if box:
            end_x = box["x"] + box["width"]/2 + random.uniform(-5, 5)
            end_y = box["y"] + box["height"]/2 + random.uniform(-5, 5)
        else:
            end_x, end_y = random.randint(0, viewport["width"]), random.randint(0, viewport["height"])
    else:
        end_x, end_y = random.randint(0, viewport["width"]), random.randint(0, viewport["height"])
    steps = random.randint(10, 20)
    ctrl_x, ctrl_y = random.randint(0, viewport["width"]), random.randint(0, viewport["height"])
    for i in range(steps + 1):
        t = i / steps
        x = (1-t)**2 * start_x + 2(1-t)t * ctrl_x + t**2 * end_x
        y = (1-t)**2 * start_y + 2(1-t)t * ctrl_y + t**2 * end_y
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.005, 0.02))

async def human_scroll(page):
    viewport = page.viewport_size
    if not viewport: return
    scrolls = random.randint(2, 5)
    for _ in range(scrolls):
        await page.mouse.wheel(0, random.randint(100, 500))
        await asyncio.sleep(random.uniform(0.3, 1.0))

async def fetch_gmail_code(gmail_addr: str, app_pass: str, timeout: int = 120) -> Optional[str]:
    def _imap_fetch():
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            imap.login(gmail_addr, app_pass)
            imap.select("INBOX")
            since = (datetime.now()).strftime("%d-%b-%Y")
            _, msgs = imap.search(None, f'(UNSEEN SINCE {since} FROM "github")')
            for num in msgs[0].split()[-10:]:
                _, data = imap.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="ignore")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="ignore")
                code = re.search(r"\b(\d{6,8})\b", body)
                if code:
                    imap.store(num, "+FLAGS", "\\Seen")
                    imap.close()
                    imap.logout()
                    return code.group(1)
            imap.close()
            imap.logout()
        except Exception as e:
            log.error(f"IMAP error: {e}")
        return None
    
    loop = asyncio.get_event_loop()
    for _ in range(timeout // 5):
        code = await loop.run_in_executor(None, _imap_fetch)
        if code: return code
        await asyncio.sleep(5)
    return None

async def classify_failure(e: Exception, page) -> str:
    try:
        html = await page.content()
        html_lower = html.lower()
        if "rate limit" in html_lower or "too many requests" in html_lower:
            return "rate_limit"
        if "captcha" in html_lower or "hcaptcha" in html_lower:
            return "captcha"
        if "data-dome" in html_lower or "datadome" in html_lower:
            return "datadome_timeout"
        if "blocked" in html_lower or "access denied" in html_lower:
            return "proxy_blocked"
    except: pass
    return "unknown"

async def wait_for_datadome(page, max_wait=90) -> bool:
    start = asyncio.get_event_loop().time()
    challenge_detected = False
    while asyncio.get_event_loop().time() - start < max_wait:
        html = await page.content()
        if 'data-dome' in html.lower() or 'captcha' in html.lower():
            challenge_detected = True
            await human_mouse_move(page)
            await human_scroll(page)
            await human_delay(3000, 6000)
        elif 'input[name="login"]' in html:
            if challenge_detected:
                log.info("DataDome cleared")
            return True
        await human_delay(1000, 2000)
    return False

async def create_one_account(email: str, proxy: Proxy) -> Optional[Account]:
    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    password = ''.join(random.choices(string.ascii_letters + string.digits + "!@#$%", k=16))
    locale, tz = proxy_locale(proxy)
    
    await rate_limit_wait(proxy.url)
    
    try:
        async with camoufox.AsyncNewBrowserContext(
            headless=True,
            proxy={"server": proxy.url},
            humanize=True,
            locale=locale,
            timezone=tz,
        ) as context:
            page = await context.new_page()
            
            await page.goto("https://github.com/signup", wait_until="domcontentloaded", timeout=60000)
            await human_delay(1000, 2000)
            
            if not await wait_for_datadome(page):
                raise Exception("DataDome challenge timeout")
            
            await human_type(page, 'input[name="login"]', username)
            await human_mouse_move(page, 'input[name="email"]')
            await human_type(page, 'input[name="email"]', email)
            await human_mouse_move(page, 'input[name="password"]')
            await human_type(page, 'input[name="password"]', password)
            
            await human_mouse_move(page, 'button[type="submit"]')
            await page.click('button[type="submit"]:has-text("Continue")')
            await page.wait_for_load_state("networkidle", timeout=30000)
            
            code = await fetch_gmail_code(BASE_GMAIL, GMAIL_APP_PASS)
            if not code:
                raise Exception("Email code timeout")
            
            await human_type(page, 'input[name="code"]', code)
            await human_mouse_move(page, 'button[type="submit"]')
            await page.click('button[type="submit"]:has-text("Continue")')
            await page.wait_for_load_state("networkidle", timeout=30000)
            
            try:
                await page.click('button:has-text("Skip personalization")', timeout=5000)
            except: pass
            
            account = Account(
                username=username, email=email, password=password,
                proxy=proxy.url, created_at=datetime.now().isoformat()
            )
            proxy.success_count += 1
            proxy.last_used = datetime.now().isoformat()
            return account
            
    except Exception as e:
        fail_type = await classify_failure(e, page) if 'page' in locals() else "unknown"
        log.error(f"Account creation failed [{fail_type}]: {e}")
        proxy.fail_count += 1
        if proxy.fail_count >= 3:
            proxy.healthy = False
        return None

async def warmup_account(account: Account, proxy: Proxy):
    """Post-creation activity to avoid instant ban"""
    try:
        locale, tz = proxy_locale(proxy)
        async with camoufox.AsyncNewBrowserContext(
            headless=True, proxy={"server": proxy.url}, humanize=True,
            locale=locale, timezone=tz
        ) as ctx:
            page = await ctx.new_page()
            await page.goto("https://github.com/login")
            await human_type(page, 'input[name="login"]', account.username)
            await human_type(page, 'input[name="password"]', account.password)
            await page.click('input[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=30000)
            
            # Browse trending
            await page.goto("https://github.com/trending")
            await human_scroll(page)
            await human_delay(3000, 5000)
            
            # Star 2-3 repos
            stars = await page.locator('button[aria-label*="Star"]').all()
            for star in random.sample(stars, min(3, len(stars))):
                await human_mouse_move(page, star)
                await star.click()
                await human_delay(1000, 2000)
            
            account.status = "warmed"
            account.warmed_at = datetime.now().isoformat()
            state.stats["warmed"] += 1
            save_state()
            log.info(f"Warmed: {account.username}")
    except Exception as e:
        log.error(f"Warmup failed for {account.username}: {e}")

async def creation_worker(task_id: int, count: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    global state
    async with task_lock:
        if state.running:
            await update.message.reply_text("⚠️ Task already running")
            return
        state.running = True
        state.current_task = {"id": task_id, "total": count, "done": 0, "success": 0, "failed": 0}
        save_state()
    
    healthy_proxies = [p for p in proxies if p.healthy]
    if not healthy_proxies:
        await update.message.reply_text("❌ No healthy proxies available")
        state.running = False
        state.current_task = None
        save_state()
        return
    
    variants = gmail_variants(BASE_GMAIL, min(count, len(healthy_proxies)))
    semaphore = asyncio.Semaphore(2)
    
    async def create_with_sem(email, proxy):
        async with semaphore:
            return await create_one_account(email, proxy)
    
    tasks = [create_with_sem(email, proxy) for email, proxy in zip(variants, healthy_proxies[:count])]
    
    for coro in asyncio.as_completed(tasks):
        if not state.running:
            break
        account = await coro
        if not state.current_task:
            break
        state.current_task["done"] += 1
        if account:
            accounts.append(account)
            state.current_task["success"] += 1
            state.stats["total"] += 1
            state.stats["success"] += 1
            msg = f"✅ #{state.current_task['done']}/{count} — {account.username} ({account.email})"
            # Warmup in background
            proxy = next(p for p in proxies if p.url == account.proxy)
            context.application.create_task(warmup_account(account, proxy))
        else:
            state.current_task["failed"] += 1
            state.stats["total"] += 1
            state.stats["failed"] += 1
            msg = f"❌ #{state.current_task['done']}/{count} — Failed"
        
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)
        except: pass
        save_state()
    
    final_success = state.current_task["success"] if state.current_task else 0
    final_failed = state.current_task["failed"] if state.current_task else 0
    state.running = False
    state.current_task = None
    save_state()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🏁 Task complete. Success: {final_success}, Failed: {final_failed}"
    )

def auth_check(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    kb = [
        [InlineKeyboardButton("🚀 Create Accounts", callback_data="create"),
         InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("🔧 Proxies", callback_data="proxies"),
         InlineKeyboardButton("📁 Accounts", callback_data="accounts")],
        [InlineKeyboardButton("🔍 Check Proxies", callback_data="check_proxies"),
         InlineKeyboardButton("🛑 Stop Task", callback_data="stop")],
    ]
    await update.message.reply_text(
        "🤖 GitHub Account Creator Bot\nReady for commands.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "create":
        await query.edit_message_text("Send: /create <count>\nExample: /create 10")
    elif data == "stats":
        txt = f"""📊 Stats
Total: {state.stats['total']}
✅ Success: {state.stats['success']}
❌ Failed: {state.stats['failed']}
🚫 Banned: {state.stats['banned']}
🔥 Warmed: {state.stats['warmed']}
Proxies: {len(proxies)} ({sum(1 for p in proxies if p.healthy)} healthy)
Accounts saved: {len(accounts)}
Running: {'Yes' if state.running else 'No'}"""
        await query.edit_message_text(txt)
    elif data == "proxies":
        txt = "🔧 Proxies:\n"
        for i, p in enumerate(proxies[:20]):
            status = "🟢" if p.healthy else "🔴"
            txt += f"{i+1}. {status} {p.scheme}://{p.host}:{p.port} [{p.country}] (✓{p.success_count} ✗{p.fail_count})\n"
        if len(proxies) > 20: txt += f"... and {len(proxies)-20} more"
        await query.edit_message_text(txt)
    elif data == "accounts":
        txt = "📁 Recent Accounts:\n"
        for a in accounts[-15:]:
            warm = "🔥" if a.status == "warmed" else "❄️"
            txt += f"{warm} {a.username} | {a.email} | {a.status} | {a.created_at[:10]}\n"
        if not accounts: txt += "None yet"
        await query.edit_message_text(txt)
    elif data == "check_proxies":
        await query.edit_message_text("🔍 Checking proxies...")
        context.application.create_task(check_proxies_task(query, context))
    elif data == "stop":
        state.running = False
        save_state()
        await query.edit_message_text("🛑 Stop signal sent")

async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    if state.running:
        await update.message.reply_text("⚠️ Task already running")
        return
    try:
        count = int(context.args[0]) if context.args else 10
        count = min(max(count, 1), 100)
    except:
        await update.message.reply_text("Usage: /create <count 1-100>")
        return
    await update.message.reply_text(f"🚀 Starting creation of {count} accounts...")
    context.application.create_task(creation_worker(random.randint(1000,9999), count, update, context))

async def cmd_addproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    if not context.args:
        await update.message.reply_text("Usage: /addproxy http://user:pass@host:port [country=US]")
        return
    for line in context.args:
        try:
            country = "US"
            if "country=" in line:
                line, country = line.split("country=", 1)
            if "://" in line:
                scheme, rest = line.split("://", 1)
            else:
                scheme, rest = "http", line
            if "@" in rest:
                auth, addr = rest.split("@", 1)
                user, password = auth.split(":", 1)
            else:
                user = password = None
                addr = rest
            host, port = addr.rsplit(":", 1)
            proxy = Proxy(host, int(port), user, password, scheme, country=country.upper())
            if not any(p.host == host and p.port == port for p in proxies):
                proxies.append(proxy)
        except Exception as e:
            log.error(f"Add proxy failed: {e}")
    save_state()
    await update.message.reply_text(f"✅ Proxies updated. Total: {len(proxies)}")

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    if not accounts:
        await update.message.reply_text("No accounts to export")
        return
    export_file = f"accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(export_file, "w") as f:
        for a in accounts:
            f.write(f"{a.username}:{a.password}:{a.email}:{a.proxy}:{a.status}\n")
    await update.message.reply_document(document=open(export_file, "rb"))

async def check_proxies_task(query, context):
    results = []
    for p in proxies:
        try:
            locale, tz = proxy_locale(p)
            async with camoufox.AsyncNewBrowserContext(
                headless=True, proxy={"server": p.url}, humanize=True,
                locale=locale, timezone=tz
            ) as ctx:
                page = await ctx.new_page()
                await page.goto("https://github.com", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
                html = await page.content()
                if "Sign in" in html or "Sign up" in html:
                    p.healthy = True
                    results.append(f"✅ {p.host}:{p.port} [{p.country}]")
                else:
                    p.healthy = False
                    results.append(f"❌ {p.host}:{p.port} [{p.country}] - blocked")
        except Exception as e:
            p.healthy = False
            results.append(f"❌ {p.host}:{p.port} [{p.country}] - {str(e)[:50]}")
    save_state()
    await context.bot.send_message(chat_id=query.message.chat_id, text="\n".join(results[:30]))

async def post_init(app: Application):
    load_state()
    log.info("Bot started, state loaded")

async def post_shutdown(app: Application):
    save_state()
    log.info("Bot stopped, state saved")

def main():
    if not BOT_TOKEN or not ADMIN_IDS:
        log.error("BOT_TOKEN and ADMIN_IDS required in .env")
        return
    if not BASE_GMAIL or not GMAIL_APP_PASS:
        log.error("BASE_GMAIL and GMAIL_APP_PASS required in .env")
        return
    
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("create", cmd_create))
    app.add_handler(CommandHandler("addproxy", cmd_addproxy))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    log.info("Bot polling...")
    app.run_polling(drop_pending_updates=True)

if name == "main":
    main()
