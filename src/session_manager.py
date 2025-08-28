import os
import json
import asyncio
import argparse
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

"""Session Manager

Purpose:
  Provide a robust, reusable session management layer for Playwright scripts.
  It will:
    1. Attempt to load & apply a previously saved session (cookies + localStorage) if still valid.
    2. If absent / invalid / expired, perform login and persist a fresh session for reuse.
    3. Support metadata (timestamps, username, version) & max-age invalidation.
    4. Capture localStorage (not always included in storage_state if not set) and rehydrate it early.

Stored File Format (JSON):
{
  "meta": {
     "version": 1,
     "created_at": ISO8601,
     "last_verified": ISO8601,
     "username": "user@example.com",
     "max_age_minutes": 480
  },
  "storage_state": {  # directly compatible with Playwright new_context(storage_state=...)
      "cookies": [...],
      "origins": [ { "origin": "https://domain", "localStorage": [ {"name": k, "value": v}, ... ] } ]
  },
  "extra": {  # optional future use
      "localStorage": {"https://domain": {"k": "v"}}
  }
}

Usage (CLI):
  python -m src.session_manager --url https://example.com --email you@ex.com --password secret \
      --login-selector "button:has-text('Login')" --session-file session.json

Integrate in other scripts:
  from src.session_manager import SessionManager
  mgr = SessionManager(url, email, password)
  browser, context, page = await mgr.ensure_session()

Security Note: For real systems, avoid storing plaintext secrets; rely on env vars.
"""

DEFAULT_MAX_AGE_MINUTES = 8 * 60  # 8 hours

@dataclass
class SessionMeta:
    version: int
    created_at: str
    last_verified: str
    username: str
    max_age_minutes: int

    @staticmethod
    def new(username: str, max_age: int) -> "SessionMeta":
        now = datetime.now(timezone.utc).isoformat()
        return SessionMeta(
            version=1,
            created_at=now,
            last_verified=now,
            username=username,
            max_age_minutes=max_age,
        )

class SessionManager:
    def __init__(
        self,
        url: str,
        email: str,
        password: str,
        session_file: str = "session.json",
        headless: bool = False,
        max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
        force_login: bool = False,
        login_wait: float = 2.0,
    ) -> None:
        self.url = url.rstrip('/') + '/'
        self.email = email
        self.password = password
        self.session_file = session_file
        self.headless = headless
        self.max_age_minutes = max_age_minutes
        self.force_login = force_login
        self.login_wait = login_wait
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._loaded_meta: Optional[SessionMeta] = None
        self._loaded_storage_state: Optional[Dict[str, Any]] = None

    # ================== Public Orchestration ==================
    async def ensure_session(self) -> tuple[Browser, BrowserContext, Page]:
        """Main entry point.
        Returns a (browser, context, page) with a valid logged-in session.
        """
        await self._launch()
        if not self.force_login:
            await self._try_load_session()
        if self._loaded_storage_state:
            # Re-apply localStorage early (best-effort) before navigation
            await self._prime_local_storage()
        page = await self._context.new_page()
        self._page = page

        if await self._validate_logged_in():
            await self._persist(verified=True)
            return self._browser, self._context, page

        # Need login
        await self._perform_login()
        if not await self._validate_logged_in():
            raise RuntimeError("Login failed: could not validate dashboard/user indicator")
        await self._persist(verified=True)
        return self._browser, self._context, page

    async def close(self):
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
        finally:
            if self._playwright:
                await self._playwright.stop()

    # ================== Internal Helpers ==================
    async def _launch(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        context_opts = {"ignore_https_errors": True, "viewport": {"width": 1280, "height": 800}}
        if self._loaded_storage_state:
            context_opts["storage_state"] = self._loaded_storage_state
        self._context = await self._browser.new_context(**context_opts)

    async def _try_load_session(self):
        if not os.path.exists(self.session_file):
            return
        try:
            with open(self.session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            meta_raw = data.get('meta')
            storage_state = data.get('storage_state')
            if not meta_raw or not storage_state:
                return
            meta = SessionMeta(**meta_raw)
            # Age check
            try:
                last = datetime.fromisoformat(meta.last_verified)
                if datetime.now(timezone.utc) - last > timedelta(minutes=meta.max_age_minutes):
                    print("Stored session expired by age policy; ignoring.")
                    return
            except Exception:
                pass
            self._loaded_meta = meta
            self._loaded_storage_state = storage_state
            print(f"Loaded session created {meta.created_at} (age OK) for user {meta.username}")
        except Exception as e:
            print(f"Failed to load session file: {e}")

    async def _prime_local_storage(self):
        """Inject localStorage entries via add_init_script BEFORE first navigation.
        Only possible once we have a context (after _launch)."""
        if not self._loaded_storage_state:
            return
        origins = self._loaded_storage_state.get('origins') or []
        for origin_entry in origins:
            origin = origin_entry.get('origin')
            items = origin_entry.get('localStorage') or []
            if not origin or not items:
                continue
            # Build JS snippet
            kv_pairs = {itm['name']: itm['value'] for itm in items if 'name' in itm}
            if not kv_pairs:
                continue
            parts = [
                "(() => { try { if (location.origin === '", origin, "') {"
            ]
            for k, v in kv_pairs.items():
                k_sanitized = str(k).replace("'", "")
                v_sanitized = str(v).replace("'", "")
                parts.append(f"localStorage.setItem('{k_sanitized}','{v_sanitized}');")
            parts.append("} } catch(e){} })();")
            script = ''.join(parts)
            await self._context.add_init_script(script)

    async def _perform_login(self):
        page = await self._context.new_page()
        self._page = page
        await page.goto(self.url)
        await page.wait_for_load_state('domcontentloaded')

        # Heuristic selectors – adjust for target app
        email_selectors = ["input[name='email']", "input[type='email']"]
        password_selectors = ["input[name='password']", "input[type='password']"]
        submit_selectors = ["button[type='submit']", "button:has-text('Login')", "button:has-text('Sign In')"]

        async def fill_first(selectors, value, mask=False):
            for sel in selectors:
                try:
                    if await page.is_visible(sel, timeout=1000):
                        await page.fill(sel, value)
                        print(f"Filled {'password' if mask else 'email'} with selector {sel}")
                        return True
                except Exception:
                    continue
            return False

        if not await fill_first(email_selectors, self.email):
            raise RuntimeError("Could not locate email field")
        if not await fill_first(password_selectors, self.password, mask=True):
            raise RuntimeError("Could not locate password field")

        clicked = False
        for sel in submit_selectors:
            try:
                if await page.is_visible(sel, timeout=1000):
                    await page.click(sel)
                    clicked = True
                    print(f"Clicked submit button {sel}")
                    break
            except Exception:
                continue
        if not clicked:
            # fallback generic button
            try:
                await page.click('button', timeout=1000)
                print("Clicked first generic button")
            except Exception:
                raise RuntimeError("Could not submit login form")

        await asyncio.sleep(self.login_wait)
        await page.wait_for_load_state('networkidle')

    async def _validate_logged_in(self) -> bool:
        page = self._page or await self._context.new_page()
        if not page.url.startswith(self.url):
            try:
                await page.goto(self.url)
            except Exception:
                pass
        indicators = [
            "text=Submit Script",
            "text=Submit Solution",
            f"text={self.email}",
        ]
        for sel in indicators:
            try:
                if await page.is_visible(sel, timeout=1200):
                    return True
            except Exception:
                continue
        return False

    async def _persist(self, verified: bool):
        if not verified:
            return
        try:
            storage = await self._context.storage_state()
            # Guarantee we include localStorage entries in storage_state origins list
            # (Playwright already does this if localStorage was touched.)
            meta = self._loaded_meta or SessionMeta.new(self.email, self.max_age_minutes)
            meta.last_verified = datetime.now(timezone.utc).isoformat()
            bundle = {
                "meta": asdict(meta),
                "storage_state": storage,
            }
            with open(self.session_file, 'w', encoding='utf-8') as f:
                json.dump(bundle, f, indent=2)
            print(f"Session persisted to {self.session_file} (cookies={len(storage.get('cookies', []))})")
        except Exception as e:
            print(f"Failed to persist session: {e}")

# ================== CLI ==================
async def cli_main(args: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description="Playwright Session Manager")
    parser.add_argument('--url', required=True)
    parser.add_argument('--email', default=os.getenv('APP_EMAIL'))
    parser.add_argument('--password', default=os.getenv('APP_PASSWORD'))
    parser.add_argument('--session-file', default='session.json')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--force-login', action='store_true')
    parser.add_argument('--max-age', type=int, default=DEFAULT_MAX_AGE_MINUTES)
    parser.add_argument('--validate-only', action='store_true', help='Exit after validation (no actions).')
    ns = parser.parse_args(args=args)

    if not ns.email or not ns.password:
        raise SystemExit("Email/password required (args or APP_EMAIL/APP_PASSWORD env vars)")

    mgr = SessionManager(
        url=ns.url,
        email=ns.email,
        password=ns.password,
        session_file=ns.session_file,
        headless=ns.headless,
        max_age_minutes=ns.max_age,
        force_login=ns.force_login,
    )

    try:
        browser, context, page = await mgr.ensure_session()
        if ns.validate_only:
            print("Session valid; exiting (validate-only mode).")
        else:
            print("Session established and ready for further automation.")
        # Minimal keep-alive demonstration
        await asyncio.sleep(0.5)
    finally:
        await mgr.close()


def main():
    asyncio.run(cli_main())


if __name__ == '__main__':
    main()
